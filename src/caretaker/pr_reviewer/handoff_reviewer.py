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
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import PRReviewerConfig
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)


# Markers per backend. Re-using a marker across backends would mean
# attempts cross-count and a re-review request can't be retargeted at a
# different agent without confusion.
CLAUDE_CODE_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-handoff -->"
OPENCODE_REVIEW_MARKER = "<!-- caretaker:pr-reviewer-opencode-handoff -->"

# Marker the agent's reply must include for caretaker to harvest the
# review payload and re-post it as a formal PR review (Reviews tab) via
# ``handoff_review_consumer``. The marker is grep-friendly so the
# upstream action's output (typically a Markdown comment) doesn't need
# a special API; agents simply emit the marker plus a fenced
# ``caretaker-review`` JSON block.
REVIEW_RESULT_MARKER = "<!-- caretaker:review-result -->"


@dataclass(frozen=True)
class HandoffReviewerSpec:
    """Per-backend strings used to compose the hand-off comment."""

    backend: str  # "claude_code" | "opencode" | …
    marker: str
    upstream_action_name: str  # human-readable name for the closing note
    label_color: str
    label_description: str


_CLAUDE_CODE = HandoffReviewerSpec(
    backend="claude_code",
    marker=CLAUDE_CODE_REVIEW_MARKER,
    upstream_action_name="anthropics/claude-code-action",
    label_color="7057ff",
    label_description="claude-code-action trigger",
)
_OPENCODE = HandoffReviewerSpec(
    backend="opencode",
    marker=OPENCODE_REVIEW_MARKER,
    upstream_action_name="sst/opencode/github",
    label_color="d04a02",
    label_description="opencode review trigger",
)

_SPECS: dict[str, HandoffReviewerSpec] = {
    _CLAUDE_CODE.backend: _CLAUDE_CODE,
    _OPENCODE.backend: _OPENCODE,
}


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


def _resolve(backend: str, config: PRReviewerConfig) -> tuple[HandoffReviewerSpec, str, str]:
    """Return the (spec, label, mention) tuple for a given backend.

    Reads the per-backend label/mention from :class:`PRReviewerConfig`
    (``claude_code_label`` / ``opencode_label`` / etc.), falling back to
    the agent's spec defaults when the operator hasn't customised them.
    """
    spec = _SPECS.get(backend)
    if spec is None:
        raise ValueError(
            f"Unsupported PR reviewer backend {backend!r}. Known backends: {', '.join(_SPECS)}"
        )
    if backend == "claude_code":
        return spec, config.claude_code_label, config.claude_code_mention
    if backend == "opencode":
        return spec, config.opencode_label, config.opencode_mention
    # Future backends would extend PRReviewerConfig with their own
    # label/mention pair; until then ``_resolve`` only knows about
    # claude_code and opencode.
    raise AssertionError(f"backend {backend!r} listed in _SPECS but no config mapping")


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
    """Apply trigger label + post hand-off comment. Returns True on success."""
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
    "OPENCODE_REVIEW_MARKER",
    "REVIEW_RESULT_MARKER",
    "HandoffReviewerSpec",
    "dispatch",
    "known_backends",
]
