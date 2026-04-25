"""Merge policy evaluation and guardrailed merge execution for PRs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.github_client.models import CheckConclusion, CheckStatus
from caretaker.guardrails import (
    CheckpointedAction,
    RollbackOutcome,
    checkpoint_and_rollback,
)
from caretaker.pr_agent.states import CIEvaluation, CIStatus, ReviewEvaluation

if TYPE_CHECKING:
    from caretaker.config import PRAgentConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


@dataclass
class MergeDecision:
    should_merge: bool
    method: str
    reason: str
    blockers: list[str]


@dataclass
class MergeExecution:
    """Structured result of :func:`perform_merge`.

    ``merged`` reflects whether the GitHub API call succeeded. When the
    rollback wrapper fires, ``merged`` stays ``True`` (the merge *did*
    land; the rollback opens a revert PR) and ``rollback_outcome`` will
    be :attr:`RollbackOutcome.ROLLED_BACK` or
    :attr:`RollbackOutcome.ROLLBACK_FAILED`.
    """

    merged: bool
    method: str
    rollback_outcome: RollbackOutcome | None = None
    reason: str = ""


def evaluate_merge(
    pr: PullRequest,
    ci: CIEvaluation,
    reviews: ReviewEvaluation,
    config: PRAgentConfig,
) -> MergeDecision:
    """Evaluate whether a PR should be auto-merged."""
    blockers: list[str] = []

    # Check CI
    if ci.status != CIStatus.PASSING:
        blockers.append(f"CI status: {ci.status.value}")

    # Check reviews
    if reviews.changes_requested:
        reviewers = [r.user.login for r in reviews.blocking_reviews]
        blockers.append(f"Changes requested by: {', '.join(reviewers)}")

    # Check merge policy
    if pr.is_copilot_pr:
        if not config.auto_merge.copilot_prs:
            blockers.append("Auto-merge disabled for Copilot PRs")
    elif pr.is_dependabot_pr:
        if not config.auto_merge.dependabot_prs:
            blockers.append("Auto-merge disabled for Dependabot PRs")
    elif pr.is_caretaker_pr:
        if not config.auto_merge.caretaker_prs:
            blockers.append("Auto-merge disabled for caretaker PRs")
    else:
        if not config.auto_merge.human_prs:
            blockers.append("Auto-merge disabled for human PRs")

    # Check draft status
    if pr.draft:
        blockers.append("PR is still a draft")

    # Check mergeability
    if pr.mergeable is False:
        blockers.append("PR has merge conflicts")

    # Check for breaking labels
    if pr.has_label("maintainer:breaking"):
        blockers.append("PR labeled as breaking — requires human review")

    should_merge = len(blockers) == 0
    reason = "All merge criteria met" if should_merge else "; ".join(blockers)

    return MergeDecision(
        should_merge=should_merge,
        method=config.auto_merge.merge_method,
        reason=reason,
        blockers=blockers,
    )


# ── Post-merge verification + rollback ────────────────────────────────────


async def _verify_base_branch_ci(
    github: GitHubClient,
    owner: str,
    repo: str,
    base_ref: str,
) -> bool | None:
    """Probe the base-branch CI: True=green, False=red, None=still pending.

    Used as the ``verify`` callable for
    :func:`caretaker.guardrails.checkpoint_and_rollback` when a merge has
    just landed. Returns:

    * ``True`` when every completed non-skipped check concluded with
      ``success`` and no check is still in-progress.
    * ``False`` the first time we see a decisive red (``failure``,
      ``timed_out``, ``action_required``).
    * ``None`` when at least one check is still queued/in-progress —
      ``checkpoint_and_rollback`` will keep polling.

    ``CheckConclusion.NEUTRAL`` / ``SKIPPED`` / ``CANCELLED`` / ``STALE``
    are treated as neither red nor green (mirrors the behaviour in
    :mod:`caretaker.pr_agent.ci_triage` for actionable vs non-actionable
    conclusions).
    """
    check_runs = await github.get_check_runs(owner, repo, base_ref)
    if not check_runs:
        # No checks means nothing to verify; treat as green so the wrapper
        # does not hold the merge open on a repo without CI.
        return True

    pending = False
    for run in check_runs:
        if run.status != CheckStatus.COMPLETED:
            pending = True
            continue
        if run.conclusion is None:
            pending = True
            continue
        if run.conclusion in (
            CheckConclusion.FAILURE,
            CheckConclusion.TIMED_OUT,
            CheckConclusion.ACTION_REQUIRED,
        ):
            return False
        # NEUTRAL / SKIPPED / CANCELLED / STALE / SUCCESS → not red.
    if pending:
        return None
    return True


async def perform_merge(
    pr: PullRequest,
    decision: MergeDecision,
    *,
    github: GitHubClient,
    config: PRAgentConfig,
    owner: str,
    repo: str,
) -> MergeExecution:
    """Merge ``pr`` and, when enabled, run the post-merge rollback guard.

    The function always calls :meth:`GitHubClient.merge_pull_request`.
    When ``config.merge_rollback.enabled`` is ``True`` it then wraps a
    5-minute polling window around the base-branch CI; if CI flips red
    inside the window the merge is reverted via a ``git revert`` PR (the
    caller-supplied rollback closure; we do not execute git operations
    from inside the guardrail — see ``docs/r-and-d/A5.md`` for the
    fast-follow-up that wires the revert automation).

    The rollback callable this function supplies is a **placeholder** —
    on verify-failure it opens an issue tagged ``caretaker:rollback`` so
    a human (or a follow-up :mod:`caretaker.self_heal_agent` pass) can
    execute the revert. Shipping the full auto-revert closure lives in a
    follow-up PR so this change stays reviewable.
    """
    from caretaker.github_client.api import GitHubAPIError  # local to avoid cycle

    if not decision.should_merge:
        return MergeExecution(
            merged=False,
            method=decision.method,
            reason=f"policy_blocked: {decision.reason}",
        )

    try:
        merged = await github.merge_pull_request(
            owner,
            repo,
            pr.number,
            method=decision.method,
        )
    except GitHubAPIError as exc:
        logger.warning("perform_merge: merge_pull_request raised %s for #%d", exc, pr.number)
        return MergeExecution(
            merged=False,
            method=decision.method,
            reason=f"api_error: {exc}",
        )

    if not merged:
        return MergeExecution(
            merged=False,
            method=decision.method,
            reason="api_returned_not_merged",
        )

    rollback_outcome: RollbackOutcome | None = None
    if config.merge_rollback.enabled:
        action = CheckpointedAction(
            repo=f"{owner}/{repo}",
            label=f"merge#{pr.number}",
        )

        async def _verify() -> bool | None:
            return await _verify_base_branch_ci(github, owner, repo, pr.base_ref or "main")

        async def _rollback() -> None:
            # Placeholder rollback: open a tracking issue so the
            # compensating revert is visible. Auto-revert-merge lands in
            # a follow-up; this keeps the guardrails PR reviewable.
            await github.create_issue(
                owner,
                repo,
                title=f"Caretaker rollback: PR #{pr.number} merge failed post-verification",
                body=(
                    f"PR #{pr.number} merged into `{pr.base_ref or 'main'}` but base-branch CI "
                    "flipped red inside the post-merge verification window. A maintainer "
                    "should review and either revert the merge or accept the regression.\n\n"
                    "Automated-by: caretaker guardrails (Agentic Design Patterns Ch. 18 "
                    "Checkpoint & Rollback).\n"
                ),
                labels=["caretaker:rollback"],
            )

        result = await checkpoint_and_rollback(
            action=action,
            verify=_verify,
            rollback=_rollback,
            window_seconds=config.merge_rollback.window_seconds,
            poll_interval_seconds=config.merge_rollback.poll_interval_seconds,
            enabled=True,
        )
        rollback_outcome = result.outcome

    return MergeExecution(
        merged=True,
        method=decision.method,
        rollback_outcome=rollback_outcome,
        reason=decision.reason,
    )


__all__ = [
    "MergeDecision",
    "MergeExecution",
    "evaluate_merge",
    "perform_merge",
]
