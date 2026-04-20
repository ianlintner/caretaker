"""PR ownership management — claim, release, and state transitions.

This module posts **one caretaker comment per PR** (the "status comment"),
identified by :data:`STATUS_COMMENT_MARKER`, and edits it in place as the PR
progresses through its lifecycle (claim → readiness updates → merge-ready →
released). Callers should use :func:`upsert_status_comment`; direct calls to
``add_issue_comment`` are reserved for other non-status comments.

Legacy markers from previous versions (``caretaker:ownership:claim``,
``caretaker:readiness:update``, ``caretaker:ownership:release``) are still
matched when searching for an existing comment, so PRs opened before this
change get their older comment edited instead of a second comment appended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from caretaker.state.models import OwnershipState, TrackedPR

if TYPE_CHECKING:
    from caretaker.config import OwnershipConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Comment, PullRequest

logger = logging.getLogger(__name__)

# Canonical marker for the single caretaker status comment per PR.
STATUS_COMMENT_MARKER = "<!-- caretaker:status -->"

# Legacy markers still recognized so PRs created before the unified status
# comment migrate cleanly: if we find an old claim/readiness/release comment,
# we edit it with the new body (which carries STATUS_COMMENT_MARKER).
_LEGACY_STATUS_MARKERS: tuple[str, ...] = (
    "<!-- caretaker:ownership:claim -->",
    "<!-- caretaker:readiness:update -->",
    "<!-- caretaker:ownership:release -->",
)


async def find_status_comment(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> Comment | None:
    """Return the caretaker status comment on a PR, if any.

    Prefers the new :data:`STATUS_COMMENT_MARKER`; falls back to any legacy
    marker so that pre-migration PRs can be updated in place instead of
    accumulating a second comment.

    When multiple matching comments exist (leftover from the pre-idempotency
    bug), the one with the highest id (most recent) is returned.
    """
    comments = await github.get_pr_comments(owner, repo, pr_number)
    match: Comment | None = None
    for c in comments:
        body = c.body or ""
        is_status = STATUS_COMMENT_MARKER in body or any(m in body for m in _LEGACY_STATUS_MARKERS)
        if is_status and (match is None or c.id > match.id):
            match = c
    return match


async def upsert_status_comment(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
) -> None:
    """Post or edit the single caretaker status comment for a PR.

    If no status comment exists, a new one is posted. Otherwise the existing
    comment is edited in place (or left untouched if the body is identical).
    """
    existing = await find_status_comment(github, owner, repo, pr_number)
    if existing is None:
        await github.add_issue_comment(owner, repo, pr_number, body)
        return
    if (existing.body or "").strip() == body.strip():
        return
    await github.edit_issue_comment(owner, repo, existing.id, body)


_ARCHIVED_BODY = "*[archived — superseded by caretaker status comment above]*"


async def compact_legacy_comments(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> int:
    """Collapse multiple caretaker-marker comments down to one (the newest).

    Pre-#403 caretaker versions posted a fresh ``caretaker:ownership:claim``
    and ``caretaker:readiness:update`` comment on every cycle. PRs created
    under those versions can carry dozens of stale duplicates. This function
    keeps the highest-id matching comment (which the regular upsert path will
    edit going forward) and removes the older ones via ``DELETE``. If a
    delete fails (permissions, etc.) the body is rewritten to a one-line
    archived marker so the duplicate at least stops being visual noise.

    Returns the number of older comments successfully deleted or archived.
    """
    comments = await github.get_pr_comments(owner, repo, pr_number)
    matches = [
        c
        for c in comments
        if (c.body or "")
        and (STATUS_COMMENT_MARKER in c.body or any(m in c.body for m in _LEGACY_STATUS_MARKERS))
    ]
    if len(matches) <= 1:
        return 0

    matches.sort(key=lambda c: c.id)
    *to_remove, _keeper = matches
    removed = 0
    for comment in to_remove:
        try:
            await github.delete_issue_comment(owner, repo, comment.id)
            removed += 1
            continue
        except Exception as e:
            logger.warning(
                "PR #%d: failed to delete legacy caretaker comment %d (%s); "
                "falling back to archive edit",
                pr_number,
                comment.id,
                e,
            )
        try:
            await github.edit_issue_comment(owner, repo, comment.id, _ARCHIVED_BODY)
            removed += 1
        except Exception as e:
            logger.warning(
                "PR #%d: failed to archive legacy caretaker comment %d: %s",
                pr_number,
                comment.id,
                e,
            )
    return removed


@dataclass
class OwnershipClaim:
    """Result of attempting to acquire or release ownership."""

    claimed: bool
    released: bool
    reason: str
    previous_state: OwnershipState


def should_auto_claim(
    pr: PullRequest,
    config: OwnershipConfig,
) -> bool:
    """Determine if Caretaker should automatically claim ownership of a PR."""
    if not config.enabled:
        return False

    if pr.is_copilot_pr:
        return config.auto_claim.copilot_prs

    if pr.is_dependabot_pr:
        return config.auto_claim.dependabot_prs

    if config.auto_claim.human_prs:
        return True

    return bool(pr.has_label(config.label))


def should_release_ownership(
    pr: PullRequest,
    tracking: TrackedPR,
    reason: str,
) -> bool:
    """Determine if Caretaker should release ownership of a PR."""
    if tracking.ownership_state != OwnershipState.OWNED:
        return False

    if reason == "escalated":
        return True
    if reason == "merged":
        return bool(pr.merged)
    if reason == "closed":
        state_value = getattr(pr.state, "value", pr.state)
        return state_value == "closed"

    return bool(pr.merged)


async def claim_ownership(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr: PullRequest,
    tracking: TrackedPR,
    ownership_config: OwnershipConfig,
) -> OwnershipClaim:
    """Attempt to acquire ownership of a PR and post the initial status comment."""
    previous_state = tracking.ownership_state

    if tracking.ownership_state == OwnershipState.OWNED:
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Already owned",
            previous_state=previous_state,
        )

    if not should_auto_claim(pr, ownership_config):
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Not eligible for auto-claim",
            previous_state=previous_state,
        )

    tracking.ownership_state = OwnershipState.OWNED
    tracking.ownership_acquired_at = datetime.now(UTC)
    tracking.owned_by = "caretaker"

    try:
        await github.add_labels(owner, repo, pr.number, [ownership_config.label])
    except Exception as e:
        logger.warning("Failed to add ownership label: %s", e)

    try:
        await upsert_status_comment(
            github, owner, repo, pr.number, build_status_comment(pr, tracking)
        )
    except Exception as e:
        logger.warning("Failed to post caretaker status comment: %s", e)

    logger.info(
        "PR #%d: Caretaker claimed ownership (was: %s)",
        pr.number,
        previous_state.value,
    )

    return OwnershipClaim(
        claimed=True,
        released=False,
        reason="Ownership acquired",
        previous_state=previous_state,
    )


async def release_ownership(
    github: GitHubClient,
    owner: str,
    repo: str,
    pr: PullRequest,
    tracking: TrackedPR,
    ownership_config: OwnershipConfig,
    reason: str = "release",
) -> OwnershipClaim:
    """Release ownership of a PR and flip the status comment to its terminal body."""
    previous_state = tracking.ownership_state

    if tracking.ownership_state not in (OwnershipState.OWNED, OwnershipState.ESCALATED):
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Not owned",
            previous_state=previous_state,
        )

    tracking.ownership_state = OwnershipState.RELEASED
    tracking.ownership_released_at = datetime.now(UTC)

    try:
        body = build_status_comment(pr, tracking, release_reason=reason)
        await upsert_status_comment(github, owner, repo, pr.number, body)
    except Exception as e:
        logger.warning("Failed to update caretaker status comment on release: %s", e)

    logger.info(
        "PR #%d: Caretaker released ownership (was: %s, reason: %s)",
        pr.number,
        previous_state.value,
        reason,
    )

    return OwnershipClaim(
        claimed=False,
        released=True,
        reason=f"Ownership released: {reason}",
        previous_state=previous_state,
    )


def _status_line(pr: PullRequest, tracking: TrackedPR, release_reason: str | None) -> str:
    """Render the one-line status heading for the status comment."""
    if release_reason:
        if bool(getattr(pr, "merged", False)):
            return "**Status:** 🎉 Merged — caretaker has released ownership"
        pr_state = getattr(pr.state, "value", pr.state)
        if pr_state == "closed":
            return "**Status:** 🚫 Closed without merge — caretaker has released ownership"
        if release_reason == "PR escalated" or tracking.escalated:
            return "**Status:** ⚠️ Escalated to maintainers — caretaker has released ownership"
        return f"**Status:** 🔓 Released — {release_reason}"

    if tracking.readiness_score >= 1.0:
        return "**Status:** ✅ Ready for merge — all requirements satisfied"
    return "**Status:** ⏳ Monitoring — awaiting requirements"


def build_status_comment(
    pr: PullRequest,
    tracking: TrackedPR,
    release_reason: str | None = None,
) -> str:
    """Render the unified caretaker status comment body.

    This single body is edited in place as the PR progresses — there is no
    separate claim / readiness / release comment. The header and status line
    reflect the current phase (monitoring / ready for merge / released).
    """
    blockers = set(tracking.readiness_blockers)
    mergeability_points = (
        0 if {"draft_pr", "merge_conflict", "breaking_change", "manual_hold"} & blockers else 10
    )
    automated_points = 0 if "automated_feedback_unaddressed" in blockers else 20
    review_points = 0 if {"required_review_missing", "changes_requested"} & blockers else 30
    ci_points = 0 if {"ci_pending", "ci_failing"} & blockers else 40
    total_pct = int(tracking.readiness_score * 100)

    blockers_section = (
        "None — PR is ready!"
        if not tracking.readiness_blockers
        else "\n".join(f"- `{b}`" for b in tracking.readiness_blockers)
    )

    ownership_lines = [f"- **Owner:** {tracking.owned_by}"]
    if tracking.ownership_acquired_at:
        ownership_lines.append(
            f"- **Claimed:** {tracking.ownership_acquired_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )
    if tracking.ownership_released_at:
        ownership_lines.append(
            f"- **Released:** {tracking.ownership_released_at.strftime('%Y-%m-%d %H:%M UTC')} "
            f"({release_reason or 'released'})"
        )
        ownership_lines.append(f"- **Duration:** {format_duration(tracking)}")

    ownership_block = "\n".join(ownership_lines)

    return f"""{STATUS_COMMENT_MARKER}

## 🏠 Caretaker Status

{_status_line(pr, tracking, release_reason)}

**Readiness Score:** {total_pct}%

### Readiness Breakdown

| Component | Score |
|-----------|-------|
| Mergeable & non-draft | {mergeability_points}% |
| Automated feedback | {automated_points}% |
| Reviews approved | {review_points}% |
| CI passing | {ci_points}% |
| **Total** | **{total_pct}%** |

### Blockers

{blockers_section}

### Ownership

{ownership_block}

---
*This comment is edited in place as the PR progresses. Automated by [Caretaker](https://github.com/ianlintner/caretaker).*
"""


def format_duration(tracking: TrackedPR) -> str:
    """Format the ownership duration as a human-readable string."""
    if not tracking.ownership_acquired_at or not tracking.ownership_released_at:
        return "unknown"
    duration = tracking.ownership_released_at - tracking.ownership_acquired_at
    if duration.days > 0:
        return f"{duration.days}d {duration.seconds // 3600}h"
    if duration.seconds >= 3600:
        return f"{duration.seconds // 3600}h {(duration.seconds % 3600) // 60}m"
    if duration.seconds >= 60:
        return f"{duration.seconds // 60}m"
    return f"{duration.seconds}s"


def get_readiness_check_title(tracking: TrackedPR) -> str:
    """Get a short title for the readiness check based on current state."""
    if tracking.readiness_score >= 1.0:
        return "PR is ready for merge"
    if "ci_pending" in tracking.readiness_blockers or "ci_failing" in tracking.readiness_blockers:
        return "Waiting for CI"
    if (
        "required_review_missing" in tracking.readiness_blockers
        or "changes_requested" in tracking.readiness_blockers
    ):
        return "Awaiting review approval"
    if "draft_pr" in tracking.readiness_blockers:
        return "PR is a draft"
    if "manual_hold" in tracking.readiness_blockers:
        return "Manual hold placed"
    return f"Readiness: {int(tracking.readiness_score * 100)}%"


def get_readiness_check_summary(tracking: TrackedPR) -> str:
    """Get a summary for the readiness check output."""
    lines = [
        f"**Readiness Score:** {int(tracking.readiness_score * 100)}%",
        "",
    ]

    if tracking.readiness_blockers:
        lines.append("**Blockers:**")
        for blocker in tracking.readiness_blockers:
            lines.append(f"- `{blocker}`")
    else:
        lines.append("**Status:** ✅ All requirements met — PR is ready for merge!")

    lines.append("")
    lines.append(f"**Summary:** {tracking.readiness_summary}")

    return "\n".join(lines)
