"""PR ownership management — claim, release, and state transitions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from caretaker.state.models import OwnershipState, TrackedPR

if TYPE_CHECKING:
    from caretaker.config import OwnershipConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


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
    """Determine if Caretaker should automatically claim ownership of a PR.

    Args:
        pr: The pull request to evaluate
        config: Ownership configuration

    Returns:
        True if Caretaker should auto-claim this PR
    """
    if not config.enabled:
        return False

    # Auto-claim Copilot PRs if configured
    if pr.is_copilot_pr:
        return config.auto_claim.copilot_prs

    # Auto-claim Dependabot PRs if configured
    if pr.is_dependabot_pr:
        return config.auto_claim.dependabot_prs

    # Human PRs require manual `caretaker:owned` label unless auto-claim is enabled
    if config.auto_claim.human_prs:
        return True

    # Check for explicit ownership label
    return bool(pr.has_label(config.label))


def should_release_ownership(
    pr: PullRequest,
    tracking: TrackedPR,
    reason: str,
) -> bool:
    """Determine if Caretaker should release ownership of a PR.

    Args:
        pr: The pull request
        tracking: Current tracking state
        reason: The reason for checking release (e.g., 'merged', 'closed', 'escalated')

    Returns:
        True if ownership should be released
    """
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
    """Attempt to acquire ownership of a PR.

    Args:
        github: GitHub client
        owner: Repository owner
        repo: Repository name
        pr: The pull request
        tracking: Current tracking state
        ownership_config: Ownership configuration

    Returns:
        OwnershipClaim with the result of the claim attempt
    """
    previous_state = tracking.ownership_state

    # Already owned by us
    if tracking.ownership_state == OwnershipState.OWNED:
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Already owned",
            previous_state=previous_state,
        )

    # Check if we should auto-claim
    if not should_auto_claim(pr, ownership_config):
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Not eligible for auto-claim",
            previous_state=previous_state,
        )

    # Claim the PR
    tracking.ownership_state = OwnershipState.OWNED
    tracking.ownership_acquired_at = datetime.now(UTC)
    tracking.owned_by = "caretaker"

    # Add ownership label
    try:
        await github.add_labels(owner, repo, pr.number, [ownership_config.label])
    except Exception as e:
        logger.warning("Failed to add ownership label: %s", e)

    # Post ownership claim comment
    comment_body = build_ownership_claim_comment(pr, tracking)
    try:
        await github.add_issue_comment(owner, repo, pr.number, comment_body)
    except Exception as e:
        logger.warning("Failed to post ownership claim comment: %s", e)

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
    """Release ownership of a PR.

    Args:
        github: GitHub client
        owner: Repository owner
        repo: Repository name
        pr: The pull request
        tracking: Current tracking state
        ownership_config: Ownership configuration
        reason: Reason for release

    Returns:
        OwnershipClaim with the result
    """
    previous_state = tracking.ownership_state

    if tracking.ownership_state not in (OwnershipState.OWNED, OwnershipState.ESCALATED):
        return OwnershipClaim(
            claimed=False,
            released=False,
            reason="Not owned",
            previous_state=previous_state,
        )

    # Release ownership
    tracking.ownership_state = OwnershipState.RELEASED
    tracking.ownership_released_at = datetime.now(UTC)

    # Post release comment
    comment_body = build_ownership_release_comment(pr, tracking, reason)
    try:
        await github.add_issue_comment(owner, repo, pr.number, comment_body)
    except Exception as e:
        logger.warning("Failed to post ownership release comment: %s", e)

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


def build_ownership_claim_comment(pr: PullRequest, tracking: TrackedPR) -> str:
    """Build the comment body for ownership claim."""
    blockers = set(tracking.readiness_blockers)
    mergeability_points = (
        0 if {"draft_pr", "merge_conflict", "breaking_change", "manual_hold"} & blockers else 10
    )
    automated_points = 0 if "automated_feedback_unaddressed" in blockers else 20
    review_points = 0 if {"required_review_missing", "changes_requested"} & blockers else 30
    ci_points = 0 if {"ci_pending", "ci_failing"} & blockers else 40

    return f"""<!-- caretaker:ownership:claim -->

## 🏠 Caretaker Ownership

Caretaker has claimed ownership of this PR.

### Readiness Score

| Component | Score |
|-----------|-------|
| Mergeable & non-draft | {mergeability_points}% |
| Automated feedback | {automated_points}% |
| Reviews approved | {review_points}% |
| CI passing | {ci_points}% |
| **Total** | **{int(tracking.readiness_score * 100)}%** |

### Blockers

{
        (
            "None — PR is ready!"
            if not tracking.readiness_blockers
            else chr(10).join(f"- `{b}`" for b in tracking.readiness_blockers)
        )
    }

### What This Means

Caretaker will monitor this PR and:
- Post updates when readiness status changes
- Request fixes for CI failures or review comments
- Attempt to merge when fully ready (if auto-merge is enabled)
- Escalate to maintainers if unable to proceed

---
*This is an automated comment from [Caretaker](https://github.com/ianlintner/caretaker).*
"""


def build_ownership_release_comment(pr: PullRequest, tracking: TrackedPR, reason: str) -> str:
    """Build the comment body for ownership release."""
    blockers = ", ".join(tracking.readiness_blockers) if tracking.readiness_blockers else "none"
    return f"""<!-- caretaker:ownership:release -->

## 🏠 Caretaker Ownership Released

Caretaker has released ownership of this PR.

**Reason:** {reason}

**Final State:**
- Ownership duration: {format_duration(tracking)}
- Final readiness score: {int(tracking.readiness_score * 100)}%
- Final blockers: {blockers}

---
*This is an automated comment from [Caretaker](https://github.com/ianlintner/caretaker).*
"""


def build_readiness_comment(
    tracking: TrackedPR,
    previous_score: float | None = None,
    previous_blockers: list[str] | None = None,
) -> str:
    """Build a comment updating readiness status.

    Only call this on meaningful changes to avoid comment noise.

    Args:
        tracking: The tracked PR with current readiness state
        previous_score: The previous readiness score to compare against

    Returns:
        The comment body, or empty string if no meaningful change
    """
    score_changed = (
        previous_score is not None and abs(tracking.readiness_score - previous_score) >= 0.1
    )
    blockers_changed = previous_blockers is not None and set(previous_blockers) != set(
        tracking.readiness_blockers
    )

    if previous_score is not None and not blockers_changed and not score_changed:
        return ""

    return f"""<!-- caretaker:readiness:update -->

## 📊 PR Readiness Update

**Readiness Score:** {int(tracking.readiness_score * 100)}%

{
        (
            "**Status:** ✅ Ready for merge"
            if tracking.readiness_score >= 1.0
            else "**Status:** ⏳ Awaiting requirements"
        )
    }

### Blockers

{
        (
            "None"
            if not tracking.readiness_blockers
            else chr(10).join(f"- `{b}`" for b in tracking.readiness_blockers)
        )
    }

---
*This is an automated comment from [Caretaker](https://github.com/ianlintner/caretaker).*
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
