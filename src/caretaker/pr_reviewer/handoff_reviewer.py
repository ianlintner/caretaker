"""Hand-off PR reviewer — slow path for complex PRs.

Generalised from the original ``claude_code_reviewer`` so that the same
shape works for any registered coding agent (Claude Code, opencode, …).
The ``PRReviewerAgent`` selects which backend dispatches based on
``PRReviewerConfig.complex_reviewer``.

Each backend gets its own marker (so re-runs don't cross-count attempts)
and its own label/mention pair (so each upstream workflow listens on its
own trigger).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from caretaker.config import PRReviewerConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.pr_reviewer.inline_reviewer import ReviewResult

logger = logging.getLogger(__name__)


# Markers per backend. Re-using a marker across backends would mean
# attempts cross-count and a re-review request can't be retargeted at a
# different agent without confusion.
CLAUDE_CODE_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-handoff -->"
OPENCODE_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-opencode-handoff -->"
PR_AGENT_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-pragent-handoff -->"
CODERABBIT_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-coderabbit-handoff -->"
GREPTILE_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-greptile-handoff -->"

# Invocation models a backend can use:
#   - ``comment_trigger``: caretaker labels the PR + posts a mention
#     comment. An external action/agent picks it up and replies. The
#     response is harvested next cycle by ``handoff_review_consumer``.
#   - ``local_subprocess``: caretaker runs the backend itself (CLI or
#     library call) and posts the formal review directly via
#     ``post_review`` — no cross-cycle harvest, no external trigger.
InvocationMode = Literal["comment_trigger", "local_subprocess"]

# Marker the agent's reply must include for caretaker to harvest the
# review payload and re-post it as a formal PR review (Reviews tab) via
# ``handoff_review_consumer``. The marker is grep-friendly so the
# upstream action's output (typically a Markdown comment) doesn't need
# a special API; agents simply emit the marker plus a fenced
# ``caretaker-review`` JSON block.
REVIEW_RESULT_MARKER = "<!-- caretaker:review-result -->"


# Runner signature for ``local_subprocess`` backends. Receives the PR
# coordinates plus backend-specific config; returns a ``ReviewResult``
# ready for ``post_review``. The runner is responsible for invoking the
# external tool, capturing its output, and shaping the response — any
# subprocess errors should be raised so the agent can log + fall back.
LocalRunner = Callable[..., Awaitable["ReviewResult"]]


@dataclass(frozen=True)
class HandoffReviewerSpec:
    """Per-backend metadata for the PR-reviewer hand-off layer.

    Two invocation modes are supported (see :data:`InvocationMode`):

    * ``comment_trigger`` (default): caretaker posts a labelled comment
      and waits for an upstream agent (Claude Code, opencode) to reply.
      The reply is harvested by ``handoff_review_consumer`` next cycle.
      Only ``marker``, ``label_color``, ``label_description``, and the
      ``upstream_action_name`` are required.

    * ``local_subprocess``: caretaker runs the tool itself (e.g. the
      pr-agent CLI). ``runner`` is the async callable that produces a
      ``ReviewResult`` to be posted directly via ``post_review``.
    """

    backend: str  # "claude_code" | "opencode" | "pr_agent" | …
    marker: str
    upstream_action_name: str  # human-readable name for the closing note
    label_color: str
    label_description: str
    invocation: InvocationMode = "comment_trigger"
    runner: LocalRunner | None = None


_CLAUDE_CODE = HandoffReviewerSpec(
    backend="claude_code",
    marker=CLAUDE_CODE_REVIEW_MARKER,
    upstream_action_name="anthropics/claude-code-action",
    label_color="7057ff",
    label_description="claude-code-action trigger",
    invocation="comment_trigger",
)
_OPENCODE = HandoffReviewerSpec(
    backend="opencode",
    marker=OPENCODE_REVIEW_MARKER,
    upstream_action_name="sst/opencode/github",
    label_color="d04a02",
    label_description="opencode review trigger",
    invocation="comment_trigger",
)


def _build_specs() -> dict[str, HandoffReviewerSpec]:
    """Assemble the registry, importing local-subprocess runners lazily.

    Imports happen inside the function so a missing optional backend
    (e.g. an unwritten greptile API client) never breaks startup of the
    rest of the agent.
    """
    specs: dict[str, HandoffReviewerSpec] = {
        _CLAUDE_CODE.backend: _CLAUDE_CODE,
        _OPENCODE.backend: _OPENCODE,
    }
    # ``backends`` package depends on this module's marker constants, so
    # importing it at module top-level would create a circular import.
    from caretaker.pr_reviewer.backends import coderabbit, greptile, pr_agent

    for spec in (pr_agent.SPEC, coderabbit.SPEC, greptile.SPEC):
        specs[spec.backend] = spec
    return specs


_SPECS: dict[str, HandoffReviewerSpec] = _build_specs()


def _build_handoff_comment(
    *,
    spec: HandoffReviewerSpec,
    mention: str,
    pr_number: int,
    owner: str,
    repo: str,
    routing_reason: str,
) -> str:
    lines = [
        spec.marker,
        f"{mention} caretaker is requesting a full code review for this PR.",
        "",
        f"**Repo:** `{owner}/{repo}` · **PR:** #{pr_number}",
        f"**Routing reason:** {routing_reason}",
        "",
        "Please review this pull request for:",
        "- Correctness and logic errors",
        "- Security vulnerabilities or unsafe patterns",
        "- API contract and backward-compatibility concerns",
        "- Test coverage gaps",
        "- Any blocking issues before merge",
        "",
        "**To have your review surface in the GitHub Reviews tab** "
        "(not just an issue comment), end your reply with the marker line "
        f"`{REVIEW_RESULT_MARKER}` followed by a fenced JSON block tagged "
        "`caretaker-review`. Example:",
        "",
        "````",
        REVIEW_RESULT_MARKER,
        "```caretaker-review",
        "{",
        '  "verdict": "COMMENT",          // APPROVE | COMMENT | REQUEST_CHANGES',
        '  "summary": "1-3 sentence overall assessment.",',
        '  "comments": [                  // optional, max 8',
        '    {"path": "src/foo.py", "line": 42, "body": "..."}',
        "  ]",
        "}",
        "```",
        "````",
        "",
        "Caretaker will pick the JSON up on its next cycle and post a "
        "formal PR review (with inline comments) attributed to "
        "`the-care-taker[bot]` so it counts in the Reviews tab. If you "
        "don't include the marker, your review still posts as a regular "
        "comment, just outside the Reviews tab.",
        "",
        f"_Delegated by caretaker's PRReviewerAgent via {spec.upstream_action_name} hand-off._",
    ]
    return "\n".join(lines)


def get_spec(backend: str) -> HandoffReviewerSpec:
    """Return the registered spec for ``backend`` or raise ``ValueError``."""
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"Unsupported PR reviewer backend {backend!r}. Known backends: {', '.join(_SPECS)}"
        )
    return spec


# Per-backend (label, mention) pairs for ``comment_trigger`` backends.
# ``local_subprocess`` backends don't post a mention comment so they
# aren't keyed here. Adding a new comment_trigger backend means adding
# both a spec entry and a row here (plus the matching ``*_label`` /
# ``*_mention`` fields on PRReviewerConfig).
_COMMENT_TRIGGER_LABEL_FIELDS: dict[str, tuple[str, str]] = {
    "claude_code": ("claude_code_label", "claude_code_mention"),
    "opencode": ("opencode_label", "opencode_mention"),
    "coderabbit": ("coderabbit_label", "coderabbit_mention"),
}


def _resolve(backend: str, config: PRReviewerConfig) -> tuple[HandoffReviewerSpec, str, str]:
    """Return the (spec, label, mention) tuple for a comment_trigger backend.

    Raises ``ValueError`` for unknown backends and ``AssertionError`` if
    a ``comment_trigger`` backend has been registered without a config
    mapping (programming error — the spec table and field map must stay
    in sync).
    """
    spec = get_spec(backend)
    if spec.invocation != "comment_trigger":
        raise ValueError(
            f"backend {backend!r} uses invocation={spec.invocation!r}; "
            "comment-trigger _resolve() does not apply"
        )
    label_field, mention_field = _COMMENT_TRIGGER_LABEL_FIELDS.get(backend, ("", ""))
    if not label_field or not hasattr(config, label_field):
        raise AssertionError(
            f"backend {backend!r} listed in _SPECS as comment_trigger "
            "but no PRReviewerConfig label/mention mapping"
        )
    return spec, getattr(config, label_field), getattr(config, mention_field)


async def dispatch(
    *,
    backend: str,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    config: PRReviewerConfig,
    routing_reason: str,
) -> bool:
    """Apply trigger label + post hand-off comment. Returns True on success.

    For comment-trigger backends only. ``local_subprocess`` backends
    (e.g. pr_agent) are handled directly in ``PRReviewerAgent`` because
    they post a formal review rather than a mention comment.
    """
    try:
        spec, label, mention = _resolve(backend, config)
    except ValueError as exc:
        logger.warning("pr-reviewer: %s", exc)
        return False

    try:
        await github.ensure_label(
            owner, repo, label, color=spec.label_color, description=spec.label_description
        )
        await github.add_labels(owner, repo, pr_number, [label])
    except Exception as exc:
        logger.warning(
            "pr-reviewer(%s): failed to apply trigger label %r to %s/%s#%d: %s",
            backend,
            label,
            owner,
            repo,
            pr_number,
            exc,
        )
        return False

    comment_body = _build_handoff_comment(
        spec=spec,
        mention=mention,
        pr_number=pr_number,
        owner=owner,
        repo=repo,
        routing_reason=routing_reason,
    )
    try:
        await github.upsert_issue_comment(
            owner,
            repo,
            pr_number,
            marker=spec.marker,
            body=comment_body,
        )
    except Exception as exc:
        logger.warning(
            "pr-reviewer(%s): failed to post hand-off comment on %s/%s#%d: %s",
            backend,
            owner,
            repo,
            pr_number,
            exc,
        )
        return False

    logger.info(
        "pr-reviewer: %s hand-off dispatched for %s/%s#%d (%s)",
        backend,
        owner,
        repo,
        pr_number,
        routing_reason,
    )
    return True


def known_backends() -> list[str]:
    """Return the set of supported PR-reviewer backend names."""
    return list(_SPECS)


__all__ = [
    "CLAUDE_CODE_REVIEW_MARKER",
    "CODERABBIT_REVIEW_MARKER",
    "GREPTILE_REVIEW_MARKER",
    "HandoffReviewerSpec",
    "InvocationMode",
    "OPENCODE_REVIEW_MARKER",
    "PR_AGENT_REVIEW_MARKER",
    "REVIEW_RESULT_MARKER",
    "dispatch",
    "get_spec",
    "known_backends",
]
