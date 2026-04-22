"""Emit a single per-run issue describing GitHub token-scope gaps.

Reads the :class:`ScopeGapTracker` singleton at the end of an
orchestrator run and either creates (first occurrence) or updates (all
later occurrences) a dedicated issue in the consumer repo. The issue
uses a stable ``<!-- caretaker:scope-gap -->`` marker plus a
``caretaker:scope-gap`` label so we find the existing issue in place
rather than re-opening a fresh one every cycle — same design contract
as :meth:`GitHubClient.upsert_issue_comment`.

The issue body tells the maintainer exactly which ``permissions:``
block to paste into their workflow file, grouped by scope, with the
actual endpoints that 403'd listed underneath each scope for
transparency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .scope_gap import ScopeGapIncident, ScopeGapTracker, get_tracker

if TYPE_CHECKING:
    from .api import GitHubClient


@dataclass(frozen=True)
class _ExistingIssue:
    """Minimal view of an issue row we need to decide create-vs-update."""

    number: int
    body: str


logger = logging.getLogger(__name__)


SCOPE_GAP_ISSUE_TITLE = "[caretaker] Workflow token is missing required scopes"
SCOPE_GAP_ISSUE_MARKER = "<!-- caretaker:scope-gap -->"
SCOPE_GAP_LABEL = "caretaker:scope-gap"
SCOPE_GAP_ACTION_LABEL = "maintainer:action-required"


# ── Body rendering ─────────────────────────────────────────────────────


def _scopes_to_permissions_yaml(scope_hints: list[str]) -> str:
    """Render a ``permissions:`` YAML block covering the given hints.

    Always emits ``contents: read`` as the baseline because every
    workflow that calls caretaker needs to at least read the repo. The
    other keys are unioned in from the observed scope hints, taking
    the higher level (write beats read) when the same scope appears at
    two levels — a token with ``issues: write`` can always read too.
    """
    levels: dict[str, str] = {"contents": "read"}
    # precedence: write > read
    for hint in scope_hints:
        if ":" not in hint:
            continue
        key, _, level = hint.partition(":")
        key = key.strip()
        level = level.strip()
        if not key or not level:
            continue
        current = levels.get(key)
        if current == "write":
            continue
        if level == "write" or current is None:
            levels[key] = level
    # Deterministic ordering: contents first, then alphabetical.
    ordered_keys = ["contents"] + sorted(k for k in levels if k != "contents")
    lines = ["permissions:"]
    for key in ordered_keys:
        lines.append(f"  {key}: {levels[key]}")
    return "\n".join(lines)


def render_issue_body(
    incidents: list[ScopeGapIncident],
    *,
    owner: str,
    repo: str,
) -> str:
    """Render the full issue body for the given incident list.

    The output carries the ``<!-- caretaker:scope-gap -->`` marker so
    the writer can find and update it next run, plus a human-readable
    breakdown of every ``(scope → endpoints)`` pair and the concrete
    ``permissions:`` YAML block the consumer should paste into their
    ``.github/workflows/maintainer.yml``.
    """
    grouped: dict[str, list[ScopeGapIncident]] = {}
    for incident in incidents:
        grouped.setdefault(incident.scope_hint, []).append(incident)

    scope_hints = sorted(grouped.keys())
    yaml_block = _scopes_to_permissions_yaml(scope_hints)

    lines: list[str] = [
        SCOPE_GAP_ISSUE_MARKER,
        "",
        f"Caretaker received `403 Forbidden` (`Resource not accessible by "
        f"integration`) on one or more GitHub endpoints during the most recent "
        f"run in `{owner}/{repo}`. Each 403 means the workflow "
        "`GITHUB_TOKEN` is missing a permission scope caretaker expected.",
        "",
        "Until the token is widened, the affected agents are silently "
        "skipping their work — for example, dependabot/code-scanning/secret-"
        "scanning triage is off, and docs changelog PRs aren't being opened.",
        "",
        "### Scopes needed",
        "",
    ]
    for hint in scope_hints:
        rows = sorted(grouped[hint], key=lambda r: (r.endpoint, r.method))
        lines.append(f"- **`{hint}`**")
        for row in rows:
            lines.append(f"  - `{row.method} {row.endpoint}` (observed {row.count}x this run)")
    lines.extend(
        [
            "",
            "### Fix",
            "",
            "Paste this block into the top of "
            "`.github/workflows/maintainer.yml` (or merge it into any existing "
            "`permissions:` block):",
            "",
            "```yaml",
            yaml_block,
            "```",
            "",
            "For org-level restrictions, you may additionally need to "
            "approve the caretaker GitHub App installation for the scopes "
            "above. Once the token has them, delete this issue — caretaker "
            "will re-open it next run if any scope is still missing.",
            "",
            "---",
            "",
            "_This issue is maintained by caretaker; the body is rewritten "
            "in place every run while the gap persists. See "
            f"`{SCOPE_GAP_LABEL}` label._",
        ]
    )
    return "\n".join(lines)


# ── Issue writer ───────────────────────────────────────────────────────


async def publish_scope_gap_issue(
    github: GitHubClient,
    owner: str,
    repo: str,
    *,
    tracker: ScopeGapTracker | None = None,
    dry_run: bool = False,
) -> int | None:
    """Create or update the per-run scope-gap issue.

    Returns the issue number when a write happened (creation or edit),
    ``None`` if the tracker was empty or we were in dry-run mode.

    The function never raises — callers tend to invoke it from the
    orchestrator's tail cleanup where nothing upstream has a handle
    to recover, so any failure is logged at WARNING and swallowed.
    """
    tracker = tracker or get_tracker()
    if tracker.is_empty():
        return None

    incidents = tracker.snapshot()
    body = render_issue_body(incidents, owner=owner, repo=repo)

    if dry_run:
        logger.info(
            "Scope-gap issue would be filed in %s/%s (incidents=%d, dry_run)",
            owner,
            repo,
            len(incidents),
        )
        return None

    try:
        existing = await _find_existing_issue(github, owner, repo)
    except Exception as exc:
        logger.warning(
            "Unable to look up existing scope-gap issue in %s/%s: %s",
            owner,
            repo,
            exc,
        )
        existing = None

    try:
        if existing is None:
            await _ensure_labels(github, owner, repo)
            issue = await github.create_issue(
                owner,
                repo,
                title=SCOPE_GAP_ISSUE_TITLE,
                body=body,
                labels=[SCOPE_GAP_LABEL, SCOPE_GAP_ACTION_LABEL],
            )
            logger.info(
                "Filed scope-gap issue #%d in %s/%s (%d scopes affected)",
                issue.number,
                owner,
                repo,
                len({i.scope_hint for i in incidents}),
            )
            return issue.number

        # Found an existing tracking issue — edit in place. Skip the
        # PATCH when the body hasn't changed so we don't churn the
        # issue's updated-at every cycle.
        if existing.body.strip() == body.strip():
            logger.debug(
                "Scope-gap issue #%d body unchanged — skipping edit",
                existing.number,
            )
            return existing.number

        await github.update_issue(
            owner,
            repo,
            existing.number,
            body=body,
            state="open",
        )
        logger.info(
            "Updated scope-gap issue #%d in %s/%s",
            existing.number,
            owner,
            repo,
        )
        return existing.number
    except Exception as exc:  # pragma: no cover - defensive tail
        logger.warning(
            "Failed to upsert scope-gap issue in %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return None


async def _find_existing_issue(
    github: GitHubClient,
    owner: str,
    repo: str,
) -> _ExistingIssue | None:
    """Return the most recent open scope-gap issue, or ``None``.

    Prefers matching by label so a renamed title still finds the issue;
    falls back to a marker-substring check on the body so issues filed
    before the label was applied (or manually retitled) are still
    picked up.
    """
    # Try labeled open issues first — cheap (server-side filter).
    labeled = await github.list_issues(
        owner,
        repo,
        state="open",
        labels=SCOPE_GAP_LABEL,
    )
    for issue in labeled:
        if SCOPE_GAP_ISSUE_MARKER in (issue.body or ""):
            return _ExistingIssue(number=issue.number, body=issue.body or "")
        if issue.title == SCOPE_GAP_ISSUE_TITLE:
            return _ExistingIssue(number=issue.number, body=issue.body or "")

    # Fallback: unlabeled but titled. Don't filter by label here.
    all_open = await github.list_issues(owner, repo, state="open")
    for issue in all_open:
        if SCOPE_GAP_ISSUE_MARKER in (issue.body or ""):
            return _ExistingIssue(number=issue.number, body=issue.body or "")
    return None


async def _ensure_labels(github: GitHubClient, owner: str, repo: str) -> None:
    """Create the scope-gap labels if they don't already exist.

    Colors chosen to stand out in the issue list: red for the gap
    label, orange for the action-required label.
    """
    try:
        await github.ensure_label(
            owner,
            repo,
            SCOPE_GAP_LABEL,
            color="b60205",
            description="Caretaker detected a missing GitHub token scope.",
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("ensure_label(%s) failed: %s", SCOPE_GAP_LABEL, exc)
    try:
        await github.ensure_label(
            owner,
            repo,
            SCOPE_GAP_ACTION_LABEL,
            color="d93f0b",
            description="Caretaker needs a maintainer to take action.",
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("ensure_label(%s) failed: %s", SCOPE_GAP_ACTION_LABEL, exc)


__all__ = [
    "SCOPE_GAP_ACTION_LABEL",
    "SCOPE_GAP_ISSUE_MARKER",
    "SCOPE_GAP_ISSUE_TITLE",
    "SCOPE_GAP_LABEL",
    "publish_scope_gap_issue",
    "render_issue_body",
]
