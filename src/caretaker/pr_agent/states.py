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
    Comment,
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
    # Successful CheckRuns from configured bot reviewers (e.g. ``claude-review``)
    # that count as an approval for the readiness gate. Empty when no bot
    # CheckRun signed off. Surfaced separately from ``approving_reviews`` so
    # the status comment can render the row as "(bot)".
    bot_check_approvals: list[CheckRun] = field(default_factory=list)
    # Bot-authored review comments / issue comments whose body matched a
    # configured approval marker (e.g. "Approved", "LGTM"). Treated the
    # same as ``bot_check_approvals`` for the gate.
    bot_comment_approvals: list[Review | Comment] = field(default_factory=list)

    @property
    def has_automated_comments(self) -> bool:
        return len(self.automated_review_comments) > 0

    @property
    def has_bot_approval(self) -> bool:
        return bool(self.bot_check_approvals) or bool(self.bot_comment_approvals)


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


def evaluate_ci(
    check_runs: list[CheckRun],
    ignore_jobs: list[str] | None = None,
    caretaker_workflow_jobs: list[str] | None = None,
) -> CIEvaluation:
    """Evaluate CI status from check runs.

    ``caretaker_workflow_jobs`` carries the names of caretaker's own
    supervisor jobs (``maintainer.yml``: dispatch-guard / doctor /
    maintain / self-heal-on-failure). They are excluded from the upstream
    rollup for the same reason ``caretaker/pr-readiness`` is: caretaker
    must not gate itself.
    """
    ignore = (
        set(ignore_jobs or []) | set(caretaker_workflow_jobs or []) | _ALWAYS_IGNORED_CHECK_NAMES
    )
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


def _body_has_marker(body: str | None, markers: list[str]) -> bool:
    """Return True when ``body`` contains any case-insensitive marker.

    Markers default to common Claude / bot phrasing (``approved``, ``lgtm``).
    Match is substring, not whole-word, so phrasings like "✅ Approved!" or
    "**Approved**" both fire.
    """
    if not body or not markers:
        return False
    needle = body.lower()
    return any(m.lower() in needle for m in markers)


def evaluate_reviews(
    reviews: list[Review],
    check_runs: list[CheckRun] | None = None,
    issue_comments: list[Comment] | None = None,
    bot_check_names: list[str] | None = None,
    bot_approval_markers: list[str] | None = None,
) -> ReviewEvaluation:
    """Evaluate review status — uses latest review per reviewer.

    Bot approvals are surfaced through three independent channels, any of
    which on its own satisfies the readiness review-gate:

    1. A formal Reviews API submission with state APPROVED — the existing
       human + caretaker-app path.
    2. A successful CheckRun whose name is listed in ``bot_check_names``
       (default: ``claude-review``). This is the channel caretaker's own
       review workflow uses; without it PR #609's comment kept reporting
       ``required_review_missing`` even though the bot had signed off.
    3. A bot-authored review (state COMMENTED) or PR issue-comment whose
       body contains an approval marker like "Approved" or "LGTM".

    A formal CHANGES_REQUESTED from any reviewer still blocks regardless
    of bot approval — only humans should be able to veto.
    """
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

    # COMMENTED reviews from bots split two ways: those carrying an explicit
    # approval marker in their body land in ``bot_comment_approvals`` and
    # gate ``approved=True``; the rest stay in ``automated_review_comments``
    # and continue to flag ``automated_feedback_unaddressed`` as a blocker.
    markers = list(bot_approval_markers or [])
    automated: list[Review] = []
    bot_comment_approvals: list[Review | Comment] = []
    for r in latest_by_user.values():
        if r.state != ReviewState.COMMENTED or not is_automated(r.user.login) or not r.body:
            continue
        if _body_has_marker(r.body, markers):
            bot_comment_approvals.append(r)
        else:
            automated.append(r)

    # Issue comments (separate from the Reviews API) from bots can also
    # carry an approval marker — e.g. when a bot replies inline rather
    # than via a formal review.
    for comment in issue_comments or []:
        login = comment.user.login if comment.user else ""
        if not login or not is_automated(login):
            continue
        if _body_has_marker(comment.body, markers):
            bot_comment_approvals.append(comment)

    # CheckRun-based bot approvals: any successful run whose name is in
    # the configured allowlist counts as a single bot approval.
    bot_names = set(bot_check_names or [])
    bot_check_approvals: list[CheckRun] = []
    for cr in check_runs or []:
        if cr.name not in bot_names:
            continue
        if cr.status == CheckStatus.COMPLETED and cr.conclusion == CheckConclusion.SUCCESS:
            bot_check_approvals.append(cr)

    has_bot_approval = bool(bot_check_approvals) or bool(bot_comment_approvals)

    return ReviewEvaluation(
        approved=(len(approvals) > 0 or has_bot_approval) and len(blockers) == 0,
        changes_requested=len(blockers) > 0,
        pending=len(latest_by_user) == 0 and not has_bot_approval,
        approving_reviews=approvals,
        blocking_reviews=blockers,
        automated_review_comments=automated,
        bot_check_approvals=bot_check_approvals,
        bot_comment_approvals=bot_comment_approvals,
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

    The ``merge_opt_in_label`` (default ``caretaker:merge``) overrides the
    per-type default for any individual PR — a human can add the label
    directly or post ``@caretaker merge`` to have caretaker apply it.
    """
    if auto_merge is None:
        return True
    if pr.has_label(auto_merge.merge_opt_in_label):
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
    issue_comments: list[Comment] | None = None,
    bot_check_names: list[str] | None = None,
    bot_approval_markers: list[str] | None = None,
    caretaker_workflow_jobs: list[str] | None = None,
) -> PRStateEvaluation:
    """Full PR evaluation — determines next state and action.

    ``auto_merge`` (optional): when supplied, the ``MERGE_READY`` recommendation
    is suppressed for PR families that the configured auto-merge policy would
    reject (e.g. ``human_prs=false``). The PR falls back to ``CI_PASSING`` with
    ``await_review``, which correctly renders as "awaiting review" in the
    status comment instead of the misleading "ready for merge".

    ``bot_check_names`` / ``bot_approval_markers`` / ``issue_comments``: see
    :func:`evaluate_reviews` — let bot CheckRun successes (e.g. ``claude-review``)
    and bot comment markers ("Approved", "LGTM") satisfy the review gate.

    ``caretaker_workflow_jobs``: see :func:`evaluate_ci` — exclude caretaker's
    own supervisor-workflow jobs from the upstream-CI rollup so the agent
    cannot self-gate.
    """
    ci = evaluate_ci(check_runs, ignore_jobs, caretaker_workflow_jobs)
    review_eval = evaluate_reviews(
        reviews,
        check_runs=check_runs,
        issue_comments=issue_comments,
        bot_check_names=bot_check_names,
        bot_approval_markers=bot_approval_markers,
    )
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
    #
    # For caretaker/maintainer-bot PRs and for any PR the human has explicitly
    # opted into auto-merge (via @caretaker merge / caretaker:merge label),
    # COMMENT-type bot reviews without approval markers don't warrant a Copilot
    # dispatch — no changes were explicitly requested and the PR is either
    # machine-generated or the human already decided to merge.
    if review_eval.has_automated_comments:
        _is_opted_in = auto_merge is not None and pr.has_label(auto_merge.merge_opt_in_label)
        _skip_dispatch = _is_opted_in or pr.is_caretaker_pr or pr.is_maintainer_bot_pr
        if current_state == PRTrackingState.FIX_REQUESTED or _skip_dispatch:
            # Fix was already requested or dispatch is not warranted — proceed
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

    # Auto-approve when CI is green and no blocking reviews exist for:
    # - caretaker-authored PRs (claude/ or caretaker/ branch prefix)
    # - maintainer-bot PRs (e.g. chore/releases-json-* from github-actions[bot])
    # - any PR explicitly opted into auto-merge via caretaker:merge label
    # Not yet approved (prevents duplicate approval submissions).
    # No fix already in-flight (would re-approve while Copilot is still working).
    # The caretaker GitHub App is a different identity from the PR author, so
    # its APPROVE satisfies the required-review gate.
    if (
        ci.status == CIStatus.PASSING
        and (
            pr.is_caretaker_pr
            or pr.is_maintainer_bot_pr
            or (auto_merge is not None and pr.has_label(auto_merge.merge_opt_in_label))
        )
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
