"""PR state machine — determines transitions based on PR status."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from caretaker.github_client.models import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    PullRequest,
    Review,
    ReviewState,
)
from caretaker.pr_agent._constants import is_automated_reviewer
from caretaker.state.models import PRTrackingState

logger = logging.getLogger(__name__)


class CIStatus(StrEnum):
    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"
    MIXED = "mixed"  # some pass, some fail


@dataclass
class CIEvaluation:
    status: CIStatus
    failed_runs: list[CheckRun]
    pending_runs: list[CheckRun]
    passed_runs: list[CheckRun]
    action_required_runs: list[CheckRun]
    all_completed: bool


@dataclass
class ReviewEvaluation:
    approved: bool
    changes_requested: bool
    pending: bool
    approving_reviews: list[Review]
    blocking_reviews: list[Review]
    # Reviews with COMMENTED state from automated reviewer bots that contain
    # actionable feedback (e.g. copilot-pull-request-reviewer).
    automated_review_comments: list[Review] = field(default_factory=list)

    @property
    def has_automated_comments(self) -> bool:
        return len(self.automated_review_comments) > 0


@dataclass
class PRStateEvaluation:
    pr: PullRequest
    ci: CIEvaluation
    reviews: ReviewEvaluation
    recommended_state: PRTrackingState
    recommended_action: str


def evaluate_ci(check_runs: list[CheckRun], ignore_jobs: list[str] | None = None) -> CIEvaluation:
    """Evaluate CI status from check runs."""
    ignore = set(ignore_jobs or [])
    relevant = [cr for cr in check_runs if cr.name not in ignore]

    if not relevant:
        return CIEvaluation(
            status=CIStatus.PENDING,
            failed_runs=[],
            pending_runs=[],
            passed_runs=[],
            action_required_runs=[],
            all_completed=True,
        )

    failed = [
        cr
        for cr in relevant
        if cr.status == CheckStatus.COMPLETED
        and cr.conclusion in (CheckConclusion.FAILURE, CheckConclusion.TIMED_OUT)
    ]
    pending = [cr for cr in relevant if cr.status in (CheckStatus.QUEUED, CheckStatus.IN_PROGRESS)]
    passed = [
        cr
        for cr in relevant
        if cr.status == CheckStatus.COMPLETED and cr.conclusion == CheckConclusion.SUCCESS
    ]
    action_required = [cr for cr in relevant if cr.conclusion == CheckConclusion.ACTION_REQUIRED]
    all_completed = len(pending) == 0

    if action_required or pending:
        status = CIStatus.PENDING
    elif failed:
        status = CIStatus.FAILING if not passed else CIStatus.MIXED
    else:
        status = CIStatus.PASSING

    return CIEvaluation(
        status=status,
        failed_runs=failed,
        pending_runs=pending,
        passed_runs=passed,
        action_required_runs=action_required,
        all_completed=all_completed,
    )


def evaluate_reviews(reviews: list[Review]) -> ReviewEvaluation:
    """Evaluate review status — uses latest review per reviewer."""
    latest_by_user: dict[str, Review] = {}
    for review in reviews:
        existing = latest_by_user.get(review.user.login)
        if existing is None or (
            review.submitted_at
            and existing.submitted_at
            and review.submitted_at > existing.submitted_at
        ):
            latest_by_user[review.user.login] = review

    approvals = [r for r in latest_by_user.values() if r.state == ReviewState.APPROVED]
    blockers = [r for r in latest_by_user.values() if r.state == ReviewState.CHANGES_REQUESTED]
    automated = [
        r
        for r in latest_by_user.values()
        if r.state == ReviewState.COMMENTED
        and is_automated_reviewer(r.user.login)
        and r.body  # only reviews that carry a summary body
    ]

    return ReviewEvaluation(
        approved=len(approvals) > 0 and len(blockers) == 0,
        changes_requested=len(blockers) > 0,
        pending=len(latest_by_user) == 0,
        approving_reviews=approvals,
        blocking_reviews=blockers,
        automated_review_comments=automated,
    )


def evaluate_pr(
    pr: PullRequest,
    check_runs: list[CheckRun],
    reviews: list[Review],
    current_state: PRTrackingState,
    ignore_jobs: list[str] | None = None,
    auto_approve_workflows: bool = False,
) -> PRStateEvaluation:
    """Full PR evaluation — determines next state and action."""
    ci = evaluate_ci(check_runs, ignore_jobs)
    review_eval = evaluate_reviews(reviews)

    # State transitions
    if pr.merged:
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.MERGED,
            recommended_action="none",
        )

    if pr.state == "closed":
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.CLOSED,
            recommended_action="none",
        )

    # CI still running
    if ci.status == CIStatus.PENDING:
        if (
            ci.action_required_runs
            and auto_approve_workflows
            and (pr.is_copilot_pr or pr.is_maintainer_pr)
        ):
            return PRStateEvaluation(
                pr=pr,
                ci=ci,
                reviews=review_eval,
                recommended_state=PRTrackingState.CI_PENDING,
                recommended_action="approve_workflows",
            )
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.CI_PENDING,
            recommended_action="wait",
        )

    # CI failing
    if ci.status in (CIStatus.FAILING, CIStatus.MIXED):
        action = "wait_for_fix" if current_state == PRTrackingState.FIX_REQUESTED else "request_fix"
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.CI_FAILING,
            recommended_action=action,
        )

    # CI passing — check reviews
    if review_eval.changes_requested:
        action = (
            "wait_for_fix"
            if current_state == PRTrackingState.FIX_REQUESTED
            else "request_review_fix"
        )
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.REVIEW_CHANGES_REQUESTED,
            recommended_action=action,
        )

    # Automated reviewer bots (e.g. copilot-pull-request-reviewer) posted comments
    # that are not formal CHANGES_REQUESTED but still carry actionable feedback.
    # Once a fix has been requested, don't re-request — the old bot comments are
    # permanent and would otherwise block the PR forever.
    if review_eval.has_automated_comments:
        if current_state == PRTrackingState.FIX_REQUESTED:
            # Fix was already requested for these comments — proceed to merge check
            pass
        else:
            return PRStateEvaluation(
                pr=pr,
                ci=ci,
                reviews=review_eval,
                recommended_state=PRTrackingState.REVIEW_CHANGES_REQUESTED,
                recommended_action="request_review_fix",
            )

    if ci.status == CIStatus.PASSING and (review_eval.approved or review_eval.pending):
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            recommended_state=PRTrackingState.MERGE_READY,
            recommended_action="merge",
        )

    return PRStateEvaluation(
        pr=pr,
        ci=ci,
        reviews=review_eval,
        recommended_state=PRTrackingState.CI_PASSING,
        recommended_action="await_review",
    )
