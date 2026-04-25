"""PR state machine — determines transitions based on PR status."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.github_client.models import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    PullRequest,
    Review,
    ReviewState,
)
from caretaker.identity import is_automated
from caretaker.state.models import PRTrackingState

if TYPE_CHECKING:
    from caretaker.config import AutoMergeConfig
    from caretaker.pr_agent.readiness_llm import Readiness

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
class ReadinessEvaluation:
    score: float
    blockers: list[str]
    summary: str
    conclusion: str  # success, failure, in_progress


@dataclass
class PRStateEvaluation:
    pr: PullRequest
    ci: CIEvaluation
    reviews: ReviewEvaluation
    readiness: ReadinessEvaluation | None = None
    # Structured readiness verdict produced by
    # :func:`caretaker.pr_agent.readiness_llm.readiness_from_legacy` (off
    # mode) or :func:`evaluate_pr_readiness_llm` (shadow / enforce modes).
    # Attached ephemerally on each evaluation; not persisted to
    # :class:`~caretaker.state.models.TrackedPR`.
    readiness_verdict: Readiness | None = None
    recommended_state: PRTrackingState = PRTrackingState.DISCOVERED
    recommended_action: str = "wait"


_ALWAYS_IGNORED_CHECK_NAMES: frozenset[str] = frozenset(
    {
        # caretaker publishes its own readiness check run on every
        # evaluation cycle. Treating it as upstream CI would create a
        # self-gating deadlock: the PR state machine sees pending /
        # action_required on the check caretaker itself owns, decides to
        # wait, never transitions to ``ci_failing``, and never dispatches
        # a fix. The check is always irrelevant to external CI health —
        # it's strictly an output of caretaker's own evaluation.
        "caretaker/pr-readiness",
    }
)


def evaluate_ci(check_runs: list[CheckRun], ignore_jobs: list[str] | None = None) -> CIEvaluation:
    """Evaluate CI status from check runs."""
    ignore = set(ignore_jobs or []) | _ALWAYS_IGNORED_CHECK_NAMES
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
    pending = [
        cr
        for cr in relevant
        if cr.status
        in (
            CheckStatus.QUEUED,
            CheckStatus.IN_PROGRESS,
            CheckStatus.WAITING,
            CheckStatus.REQUESTED,
            CheckStatus.PENDING,
        )
    ]
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
        and is_automated(r.user.login)
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


def evaluate_readiness(
    pr: PullRequest,
    ci: CIEvaluation,
    review_eval: ReviewEvaluation,
    current_state: PRTrackingState,
    required_reviews: int = 1,
) -> ReadinessEvaluation:
    """Evaluate PR readiness score and blockers.

    Args:
        required_reviews: Minimum approving reviews required for the review-points
            component. When ``0`` (repo doesn't require reviews), the review
            component passes automatically and ``required_review_missing`` is not
            added as a blocker. An explicit ``changes_requested`` review still blocks.
    """
    score = 0.0
    blockers = []

    # 10%: Mergeable, non-draft, no breaking, no hold
    # pr.mergeable is None when GitHub hasn't computed it yet — treat as non-blocking.
    if (
        not pr.draft
        and pr.mergeable is not False
        and not pr.has_label("maintainer:breaking")
        and not pr.has_label("caretaker:hold")
    ):
        score += 0.10
    else:
        if pr.draft:
            blockers.append("draft_pr")
        if pr.mergeable is False:
            blockers.append("merge_conflict")
        if pr.has_label("maintainer:breaking"):
            blockers.append("breaking_change")
        if pr.has_label("caretaker:hold"):
            blockers.append("manual_hold")

    # 20%: Automated feedback addressed
    if not review_eval.has_automated_comments or current_state == PRTrackingState.FIX_REQUESTED:
        score += 0.20
    else:
        blockers.append("automated_feedback_unaddressed")

    # 30%: Required reviews satisfied. When required_reviews is 0, the repo
    # does not require approving reviews, so this component passes unless a
    # reviewer has explicitly requested changes.
    reviews_required = required_reviews > 0
    if review_eval.changes_requested:
        blockers.append("changes_requested")
    elif reviews_required and not review_eval.approved:
        blockers.append("required_review_missing")
    else:
        score += 0.30

    # 40%: CI green and no pending checks
    if ci.status == CIStatus.PASSING and ci.all_completed:
        score += 0.40
    else:
        if ci.status in (CIStatus.FAILING, CIStatus.MIXED):
            blockers.append("ci_failing")
        if not ci.all_completed:
            blockers.append("ci_pending")

    # Determine conclusion
    if not blockers:
        conclusion = "success"
        summary = "PR is ready for merge"
    elif "ci_pending" in blockers or (
        "required_review_missing" in blockers and "changes_requested" not in blockers
    ):
        conclusion = "in_progress"
        summary = f"PR pending: {', '.join(blockers)}"
    else:
        conclusion = "failure"
        summary = f"PR blocked: {', '.join(blockers)}"

    return ReadinessEvaluation(
        score=round(score, 2),
        blockers=blockers,
        summary=summary,
        conclusion=conclusion,
    )


def _auto_merge_allows(pr: PullRequest, auto_merge: AutoMergeConfig | None) -> bool:
    """Return True when the configured auto-merge policy allows this PR family.

    Used to gate the ``MERGE_READY`` recommendation: if caretaker wouldn't
    actually merge this PR under ``config.auto_merge``, then the state
    machine should not surface it as "ready for merge" in status comments —
    that's user-confusing ("ready" but nothing happens). When ``auto_merge``
    is ``None`` (caller didn't thread the config in), the gate is disabled
    for backwards compatibility.
    """
    if auto_merge is None:
        return True
    if pr.is_copilot_pr:
        return auto_merge.copilot_prs
    if pr.is_dependabot_pr:
        return auto_merge.dependabot_prs
    if pr.is_maintainer_bot_pr:
        return auto_merge.maintainer_bot_prs
    return auto_merge.human_prs


def evaluate_pr(
    pr: PullRequest,
    check_runs: list[CheckRun],
    reviews: list[Review],
    current_state: PRTrackingState,
    ignore_jobs: list[str] | None = None,
    auto_approve_workflows: bool = False,
    required_reviews: int = 1,
    auto_merge: AutoMergeConfig | None = None,
) -> PRStateEvaluation:
    """Full PR evaluation — determines next state and action.

    ``auto_merge`` (optional): when supplied, the ``MERGE_READY`` recommendation
    is suppressed for PR families that the configured auto-merge policy would
    reject (e.g. ``human_prs=false``). The PR falls back to ``CI_PASSING`` with
    ``await_review``, which correctly renders as "awaiting review" in the
    status comment instead of the misleading "ready for merge".
    """
    ci = evaluate_ci(check_runs, ignore_jobs)
    review_eval = evaluate_reviews(reviews)
    readiness = evaluate_readiness(pr, ci, review_eval, current_state, required_reviews)

    # State transitions
    if pr.merged:
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            readiness=readiness,
            recommended_state=PRTrackingState.MERGED,
            recommended_action="none",
        )

    if pr.state == "closed":
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            readiness=readiness,
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
                readiness=readiness,
                recommended_state=PRTrackingState.CI_PENDING,
                recommended_action="approve_workflows",
            )
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            readiness=readiness,
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
            readiness=readiness,
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
            readiness=readiness,
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
                readiness=readiness,
                recommended_state=PRTrackingState.REVIEW_CHANGES_REQUESTED,
                recommended_action="request_review_fix",
            )

    # Auto-approve caretaker-authored or maintainer-bot PRs when:
    # - CI is green
    # - No CHANGES_REQUESTED reviews
    # - Not yet approved (prevents duplicate approval submissions)
    # - No fix already in-flight (would re-approve while Copilot is still working)
    # The caretaker GitHub App is a different identity from the PR author, so
    # its APPROVE satisfies the required-review gate.
    # Maintainer-bot PRs (e.g. chore/releases-json-*) contain only mechanical,
    # workflow-generated changes and are equally safe to auto-approve.
    if (
        ci.status == CIStatus.PASSING
        and (pr.is_caretaker_pr or pr.is_maintainer_bot_pr)
        and not review_eval.changes_requested
        and not review_eval.approved
        and current_state != PRTrackingState.FIX_REQUESTED
    ):
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            readiness=readiness,
            recommended_state=PRTrackingState.CI_PASSING,
            recommended_action="request_review_approve",
        )

    # Cap MERGE_READY when auto-merge is disabled for this PR family —
    # otherwise the status comment says "ready for merge" even though
    # caretaker will refuse to merge (see merge.evaluate_merge). Fall through
    # to CI_PASSING / await_review so the status comment correctly renders
    # the awaiting-review state.
    if (
        ci.status == CIStatus.PASSING
        and (review_eval.approved or review_eval.pending)
        and _auto_merge_allows(pr, auto_merge)
    ):
        return PRStateEvaluation(
            pr=pr,
            ci=ci,
            reviews=review_eval,
            readiness=readiness,
            recommended_state=PRTrackingState.MERGE_READY,
            recommended_action="merge",
        )

    return PRStateEvaluation(
        pr=pr,
        ci=ci,
        reviews=review_eval,
        readiness=readiness,
        recommended_state=PRTrackingState.CI_PASSING,
        recommended_action="await_review",
    )
