"""Merge policy evaluation for PRs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from project_maintainer.config import PRAgentConfig
from project_maintainer.github_client.models import PullRequest
from project_maintainer.pr_agent.states import CIEvaluation, CIStatus, ReviewEvaluation

logger = logging.getLogger(__name__)


@dataclass
class MergeDecision:
    should_merge: bool
    method: str
    reason: str
    blockers: list[str]


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
