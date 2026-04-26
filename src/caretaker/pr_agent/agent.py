"""PR Agent — the main PR monitoring and management agent."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.causal import make_causal_marker, parent_from_body
from caretaker.config import MergeAuthorityMode
from caretaker.evolution.shadow import shadow_decision
from caretaker.github_client.api import GitHubAPIError
from caretaker.github_client.models import PRState
from caretaker.identity import is_automated
from caretaker.llm.copilot import CopilotProtocol, ResultStatus
from caretaker.pr_agent.ci_triage import FailureType, triage_failure
from caretaker.pr_agent.copilot import PRCopilotBridge
from caretaker.pr_agent.merge import evaluate_merge
from caretaker.pr_agent.ownership import (
    build_status_comment,
    claim_ownership,
    compact_legacy_comments,
    get_readiness_check_summary,
    get_readiness_check_title,
    release_ownership,
    should_release_ownership,
    upsert_status_comment,
)
from caretaker.pr_agent.readiness_llm import (
    PRReadinessContext,
    Readiness,
    evaluate_pr_readiness_llm,
    readiness_from_legacy,
)
from caretaker.pr_agent.review import ReviewVerdict, analyze_reviews, assess_review_verdict
from caretaker.pr_agent.states import (
    PRStateEvaluation,
    ReadinessEvaluation,
    evaluate_pr,
)
from caretaker.pr_agent.stuck_pr_llm import (
    PRStuckContext,
    StuckVerdict,
    evaluate_stuck_pr_llm,
    stuck_from_legacy,
)
from caretaker.state.models import OwnershipState, PRTrackingState, TrackedPR
from caretaker.tools.debug_dump import render_debug_dump

if TYPE_CHECKING:
    from caretaker.config import PRAgentConfig
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.foundry.dispatcher import ExecutorDispatcher
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest
    from caretaker.llm.router import LLMRouter
    from caretaker.memory.retriever import MemoryRetriever

logger = logging.getLogger(__name__)


def _readiness_verdicts_agree(a: Readiness, b: Readiness) -> bool:
    """Compare two :class:`Readiness` verdicts at the decision level.

    Only the top-level ``verdict`` matters for the shadow-mode
    disagreement rate — the legacy adapter can never match the LLM's
    free-text ``summary`` or ``human_reason`` fields, and blocker order
    differs by design (LLM may add ``waiting_for_upstream`` categories
    legacy cannot produce). The goal of shadow mode is to prove the LLM
    agrees on *ready vs not ready* before flipping authority; finer-
    grained matching belongs in a later milestone.
    """
    return a.verdict == b.verdict


@shadow_decision("readiness", compare=_readiness_verdicts_agree)
async def _decide_readiness(*, legacy: Any, candidate: Any, context: Any = None) -> Readiness:
    """Shadow-mode decision point wrapping the legacy + LLM readiness paths.

    The body never runs — the decorator short-circuits to ``legacy`` in
    ``off`` / ``shadow`` modes and to ``candidate`` in ``enforce`` mode.
    """
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


def _stuck_verdicts_agree(a: StuckVerdict, b: StuckVerdict) -> bool:
    """Compare two :class:`StuckVerdict` verdicts at the decision level.

    Only ``is_stuck`` and ``recommended_action`` affect downstream
    behaviour (escalate vs. wait vs. nudge vs. self-approve). The
    ``stuck_reason`` and free-text ``explanation`` are informational —
    the legacy adapter can emit only ``abandoned`` / ``not_stuck`` and
    can never match the LLM's richer reason taxonomy, so comparing on
    those fields would spam the disagreement counter without signal.
    """
    return a.is_stuck == b.is_stuck and a.recommended_action == b.recommended_action


@shadow_decision("stuck_pr", compare=_stuck_verdicts_agree)
async def _decide_stuck_pr(*, legacy: Any, candidate: Any, context: Any = None) -> StuckVerdict:
    """Shadow-mode decision point for the stuck-PR gate.

    The body never runs — the decorator short-circuits to ``legacy`` in
    ``off`` / ``shadow`` modes and to ``candidate`` in ``enforce`` mode.
    """
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


@dataclass
class PRAgentReport:
    monitored: int = 0
    merged: list[int] = field(default_factory=list)
    approved: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    escalated: list[int] = field(default_factory=list)
    fix_requested: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _mark_caretaker_touched(tracking: TrackedPR) -> None:
    """Flip the attribution booleans on a PR when caretaker takes a write action.

    Called immediately after any non-read-only GitHub operation (comment,
    label, approve, merge, close). ``last_caretaker_action_at`` is the
    anchor used by
    :func:`caretaker.state.intervention_detector.detect_pr_intervention`
    to decide whether a later human action qualifies as an operator
    rescue. We stamp the *latest* write so consecutive caretaker actions
    in the same cycle don't accidentally claim an earlier timestamp.
    """
    tracking.caretaker_touched = True
    tracking.last_caretaker_action_at = datetime.now(UTC)


class PRAgent:
    """Monitors and manages pull requests through their lifecycle."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: PRAgentConfig,
        llm_router: LLMRouter | None = None,
        insight_store: InsightStore | None = None,
        dispatcher: ExecutorDispatcher | None = None,
        memory_retriever: MemoryRetriever | None = None,
        app_id: int | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._llm = llm_router
        self._insight_store = insight_store
        # T-E2: optional cross-run memory retriever. When supplied, the
        # readiness LLM candidate injects up to three prior memory
        # snapshots into its prompt. Left as ``None`` when the
        # ``config.memory_store.retrieval_enabled`` knob is off so the
        # prompt is identical to the no-memory path byte-for-byte.
        self._memory_retriever = memory_retriever
        # The GitHub App id this process runs as.  When set, _publish_readiness_check
        # can compare against the existing check run's app_id to detect cross-App
        # ownership conflicts *before* attempting an update (avoids 403 "Invalid app_id").
        self._app_id = app_id
        self._copilot_protocol = CopilotProtocol(github, owner, repo)
        self._copilot_bridge = PRCopilotBridge(
            self._copilot_protocol,
            max_retries=config.copilot.max_retries,
            insight_store=insight_store,
            dispatcher=dispatcher,
        )

    async def run(
        self,
        tracked_prs: dict[int, TrackedPR],
        head_branch: str | None = None,
        pr_number: int | None = None,
    ) -> tuple[PRAgentReport, dict[int, TrackedPR]]:
        """Run the PR agent — evaluate all open PRs and take action.

        Args:
            tracked_prs: Current tracking state keyed by PR number.
            head_branch: Optional branch name filter. When provided, only PRs
                whose ``head_ref`` matches this value are evaluated (used to
                limit work on ``workflow_run`` events to the relevant branch).
            pr_number: Optional single PR number to evaluate. When provided,
                only that PR is fetched and processed (used for ``pull_request``,
                ``pull_request_review``, ``check_run``, and ``check_suite``
                events to avoid a full repository scan).

        Note:
            ``pr_number`` and ``head_branch`` are mutually exclusive: they are
            dispatched from different event types by the orchestrator and should
            never both be set in production.  When both are supplied ``pr_number``
            takes precedence and ``head_branch`` is ignored.
        """
        report = PRAgentReport()

        if pr_number is not None:
            if head_branch is not None:
                logger.warning(
                    "run() received both pr_number=%d and head_branch=%r; "
                    "pr_number takes precedence, head_branch will be ignored",
                    pr_number,
                    head_branch,
                )
            # Fast path: fetch only the single PR identified by the event payload.
            # This avoids a full list_pull_requests scan and the O(N) API calls
            # that come with it.
            pr = await self._github.get_pull_request(self._owner, self._repo, pr_number)
            if pr is None or pr.state != PRState.OPEN:
                # PR was closed/merged externally — nothing to do
                return report, tracked_prs
            open_prs = [pr]
        else:
            # Discover open PRs
            open_prs = await self._github.list_pull_requests(self._owner, self._repo)

            # Filter to branch of interest when provided (avoids full scan on workflow_run)
            if head_branch:
                open_prs = [pr for pr in open_prs if pr.head_ref == head_branch]

        report.monitored = len(open_prs)

        # Sync tracked PRs that are no longer open — they were merged or closed
        # externally (e.g. manually) while the orchestrator wasn't watching.
        #
        # Only do this during full-repository scans. On branch-filtered or
        # single-PR runs, open_prs only contains a subset of all PRs, so using
        # it as "all open PRs" would incorrectly classify unrelated tracked PRs
        # as closed.
        if head_branch is None and pr_number is None:
            open_pr_numbers = {pr.number for pr in open_prs}
            _terminal = {
                PRTrackingState.MERGED,
                PRTrackingState.CLOSED,
                PRTrackingState.ESCALATED,
            }
            # Use a distinct loop variable so we don't shadow the outer
            # ``pr_number`` parameter. Even though the post-loop code below
            # doesn't currently re-read it, shadowing a parameter is a latent
            # bug magnet — keep the names separate.
            for tracked_pr_number, tracked in list(tracked_prs.items()):
                if tracked_pr_number not in open_pr_numbers and tracked.state not in _terminal:
                    try:
                        closed_pr = await self._github.get_pull_request(
                            self._owner, self._repo, tracked_pr_number
                        )
                        if closed_pr is not None:
                            if closed_pr.merged:
                                tracked.state = PRTrackingState.MERGED
                                # Prefer GitHub's true merge timestamp when we don't already
                                # have one persisted from a prior cycle.
                                if tracked.merged_at is None:
                                    tracked.merged_at = closed_pr.merged_at
                                logger.info(
                                    "PR #%d: externally merged — updated tracked state",
                                    tracked_pr_number,
                                )
                            elif closed_pr.state == PRState.CLOSED:
                                tracked.state = PRTrackingState.CLOSED
                                logger.info(
                                    "PR #%d: externally closed — updated tracked state",
                                    tracked_pr_number,
                                )
                    except Exception as exc:
                        logger.debug("Could not sync state for PR #%d: %s", tracked_pr_number, exc)

        for pr in open_prs:
            try:
                tracking = tracked_prs.get(pr.number, TrackedPR(number=pr.number))
                tracking = await self._process_pr(pr, tracking, report)
                tracking.last_checked = datetime.now(UTC)
                tracked_prs[pr.number] = tracking
            except Exception as e:
                logger.error("Error processing PR #%d: %s", pr.number, e)
                report.errors.append(f"PR #{pr.number}: {e}")

        return report, tracked_prs

    @staticmethod
    def _pr_age_hours(pr: PullRequest) -> float:
        """Return the PR's open age in hours, or 0.0 if created_at is missing."""
        if pr.created_at is None:
            return 0.0
        created = pr.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created).total_seconds() / 3600.0

    def _is_pr_stuck_by_age(self, pr: PullRequest, reviews: list[Any]) -> bool:
        """Return True when the PR meets the stuck-by-age criteria.

        Stuck means: open longer than ``stuck_age_hours`` AND no human
        approval review on file. CI state is intentionally NOT considered —
        a PR that's been failing for 24h with no human attention is exactly
        what this gate catches.

        Retained as the ``legacy`` branch of the
        ``@shadow_decision("stuck_pr")`` gate in
        :meth:`_evaluate_stuck_verdict`. The ``stuck_age_hours`` threshold
        also acts as a minimum-age pre-filter: callers skip the whole
        shadow decision when the PR is younger than the threshold, so the
        LLM candidate is never called on freshly-opened PRs.
        """
        from caretaker.github_client.models import ReviewState

        age = self._pr_age_hours(pr)
        if age < self._config.stuck_age_hours:
            return False
        for review in reviews or []:
            if getattr(review, "state", None) == ReviewState.APPROVED:
                user = getattr(review, "user", None)
                login = getattr(user, "login", "") if user else ""
                # Only human approvals count — bot reviewers don't count
                if login and not is_automated(login):
                    return False
        return True

    async def _evaluate_stuck_verdict(
        self,
        pr: PullRequest,
        check_runs: list[Any],
        reviews: list[Any],
        readiness_verdict: Readiness | None,
    ) -> StuckVerdict | None:
        """Evaluate the stuck-PR gate under the ``stuck_pr`` shadow switch.

        Returns ``None`` when the PR is younger than ``stuck_age_hours``
        (the minimum-age pre-filter) — in that case neither the legacy
        heuristic nor the LLM candidate runs. Otherwise dispatches through
        ``@shadow_decision("stuck_pr")`` so behaviour tracks the Phase 2
        rollout: ``off`` returns the legacy binary verdict, ``shadow``
        returns the legacy verdict and records disagreements, ``enforce``
        returns the LLM candidate (falling through to legacy on error).
        """
        if self._config.stuck_age_hours <= 0:
            return None
        age_hours = self._pr_age_hours(pr)
        if age_hours < self._config.stuck_age_hours:
            return None

        is_stuck_legacy = self._is_pr_stuck_by_age(pr, reviews)

        last_activity: datetime | None = pr.updated_at or pr.created_at
        last_activity_hours: float | None = None
        if last_activity is not None:
            stamp = last_activity
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            last_activity_hours = (datetime.now(UTC) - stamp).total_seconds() / 3600.0

        # On a solo-maintainer repo ``required_reviews == 0`` so
        # :class:`ReadinessConfig` is the right signal — it mirrors the
        # solo check used by the readiness gate.
        collaborator_count: int | None = 1 if self._config.readiness.required_reviews == 0 else None

        async def _legacy_path() -> StuckVerdict:
            return stuck_from_legacy(is_stuck_legacy)

        async def _candidate_path() -> StuckVerdict | None:
            if self._llm is None or not self._llm.available:
                return None
            context = PRStuckContext(
                pr=pr,
                age_hours=age_hours,
                last_activity_hours=last_activity_hours,
                check_runs=list(check_runs),
                reviews=list(reviews),
                readiness_verdict=readiness_verdict,
                repo_slug=f"{self._owner}/{self._repo}",
                collaborator_count=collaborator_count,
            )
            return await evaluate_stuck_pr_llm(context, claude=self._llm.claude)

        try:
            verdict: StuckVerdict = await _decide_stuck_pr(
                legacy=_legacy_path,
                candidate=_candidate_path,
                context={
                    "pr_number": pr.number,
                    "repo_slug": f"{self._owner}/{self._repo}",
                    "age_hours": round(age_hours, 1),
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the agent
            logger.warning(
                "PR #%d: stuck-PR shadow-decision failed (%s: %s); falling back to legacy adapter",
                pr.number,
                type(exc).__name__,
                exc,
            )
            verdict = stuck_from_legacy(is_stuck_legacy)

        return verdict

    @staticmethod
    def _stuck_verdict_requires_escalation(verdict: StuckVerdict) -> bool:
        """Return True when a stuck verdict warrants the escalation path.

        Every action except ``wait`` and ``request_fix`` lands on the
        escalation path for now. ``request_fix`` is already handled by
        the CI-fix lifecycle downstream and a duplicate escalation
        comment would spam the PR. ``wait`` is, obviously, a no-op.
        The remaining actions — ``escalate``, ``nudge_reviewer``,
        ``close_stale``, ``self_approve_on_solo`` — all surface as an
        escalation with an action-specific reason so humans see *why*
        caretaker stopped touching the PR.
        """
        if not verdict.is_stuck:
            return False
        return verdict.recommended_action in {
            "escalate",
            "nudge_reviewer",
            "close_stale",
            "self_approve_on_solo",
        }

    def _stuck_escalation_reason(self, verdict: StuckVerdict) -> str:
        """Render an escalation-comment reason for a stuck verdict.

        Action-shaped so the operator sees the recommended next step,
        not just the diagnosis. Preserves the legacy reason verbatim
        when the action is ``escalate`` (the ``off`` / ``shadow`` path),
        so the comment body doesn't drift when the flag is flipped from
        ``off`` to ``shadow``.
        """
        action = verdict.recommended_action
        if action == "escalate":
            return f"Open >{self._config.stuck_age_hours}h with no human approval — needs review"
        if action == "nudge_reviewer":
            return (
                f"Stuck waiting on a review — reviewer nudge recommended ({verdict.stuck_reason})."
            )
        if action == "close_stale":
            return f"Stuck and stale — recommended action is ``close_stale``. {verdict.explanation}"
        if action == "self_approve_on_solo":
            return (
                "Solo-maintainer repo: the PR is ready but cannot clear the "
                "required-review gate without a second approver."
            )
        # Defensive: any other action falls back to the legacy wording so
        # a surprise candidate can't produce an empty or confusing reason.
        return (
            f"Stuck PR ({verdict.stuck_reason}) — recommended action "
            f"``{action}``. {verdict.explanation}"
        )

    async def _process_pr(
        self, pr: PullRequest, tracking: TrackedPR, report: PRAgentReport
    ) -> TrackedPR:
        """Process a single PR through the state machine."""
        if tracking.first_seen_at is None:
            tracking.first_seen_at = datetime.now(UTC)

        # Fetch CI status and reviews
        check_runs = await self._github.get_check_runs(self._owner, self._repo, pr.head_ref)
        reviews = await self._github.get_pr_reviews(self._owner, self._repo, pr.number)

        # Evaluate PR state (shared by both the stuck-PR gate and the
        # main action ladder below).
        evaluation = evaluate_pr(
            pr=pr,
            check_runs=check_runs,
            reviews=reviews,
            current_state=tracking.state,
            ignore_jobs=self._config.ci.ignore_jobs,
            auto_approve_workflows=self._config.ci.auto_approve_workflows,
            required_reviews=self._config.readiness.required_reviews,
            auto_merge=self._config.auto_merge,
        )

        # Phase 2 (2026-Q2 §3.1): shadow-mode migration of readiness. Off
        # mode keeps the legacy adapter authoritative; shadow runs the LLM
        # side-by-side and records disagreements; enforce promotes the LLM
        # verdict. Controlled by ``AgenticConfig.readiness.mode``.
        await self._attach_readiness_verdict(pr, evaluation, check_runs, reviews)

        # Stuck-PR gate (E1, Phase 2 §3.8): if the PR has been open long
        # enough to be a stuck candidate, run the ``stuck_pr`` shadow
        # decision. In ``off`` mode this is the legacy binary
        # age-heuristic (portfolio #4 was open 10 days; #28 was open
        # 7 days). In ``shadow`` / ``enforce`` modes an LLM candidate
        # distinguishes abandoned / ci_deadlock / awaiting_human_decision
        # / merge_queue / solo_repo_no_reviewer and picks a matching
        # action. Skipped if already escalated (terminal) or the PR is
        # closed/merged.
        #
        # Exception: Copilot-authored PRs with ``action_required`` CI runs
        # are waiting for owner workflow approval — this is normal expected
        # behaviour, not a stall. Suppress the stuck gate so caretaker
        # does not escalate while the owner has simply not yet clicked
        # "Approve and run" in the Actions tab.
        _copilot_awaiting_approval = pr.is_copilot_pr and bool(evaluation.ci.action_required_runs)
        if (
            not tracking.escalated
            and tracking.state
            not in (
                PRTrackingState.ESCALATED,
                PRTrackingState.MERGED,
                PRTrackingState.CLOSED,
            )
            and not _copilot_awaiting_approval
        ):
            stuck_verdict = await self._evaluate_stuck_verdict(
                pr, check_runs, reviews, evaluation.readiness_verdict
            )
            if stuck_verdict is not None and self._stuck_verdict_requires_escalation(stuck_verdict):
                reason = self._stuck_escalation_reason(stuck_verdict)
                await self._escalate(
                    pr,
                    reason,
                    debug_data={
                        "pr_age_hours": self._pr_age_hours(pr),
                        "stuck_age_hours": self._config.stuck_age_hours,
                        "fix_cycles": tracking.fix_cycles,
                        "copilot_attempts": tracking.copilot_attempts,
                        "stuck_reason": stuck_verdict.stuck_reason,
                        "recommended_action": stuck_verdict.recommended_action,
                        "stuck_confidence": stuck_verdict.confidence,
                    },
                )
                tracking.state = PRTrackingState.ESCALATED
                tracking.escalated = True
                _mark_caretaker_touched(tracking)
                report.escalated.append(pr.number)
                # Still flow through ownership handling so the status
                # comment transitions to "released — escalated" cleanly.
                tracking = await self._handle_ownership(pr, tracking, evaluation, report)
                return tracking

        logger.info(
            "PR #%d: %s → %s (action: %s)",
            pr.number,
            tracking.state.value,
            evaluation.recommended_state.value,
            evaluation.recommended_action,
        )

        # Track fix cycles: each FIX_REQUESTED → CI_FAILING transition is one cycle
        if (
            tracking.state == PRTrackingState.FIX_REQUESTED
            and evaluation.recommended_state == PRTrackingState.CI_FAILING
        ):
            tracking.fix_cycles += 1
            tracking.stuck_reflection_done = False  # allow re-analysis next cycle
            logger.debug("PR #%d: fix_cycles incremented to %d", pr.number, tracking.fix_cycles)

        # Set state to the recommendation first; action handlers may override
        # (e.g. FIX_REQUESTED after posting a @copilot comment).
        tracking.state = evaluation.recommended_state

        # Act on the recommendation
        match evaluation.recommended_action:
            case "approve_workflows":
                tracking = await self._handle_approve_workflows(pr, evaluation, tracking, report)
            case "merge":
                tracking = await self._handle_merge(pr, evaluation, tracking, report)
            case "request_fix":
                tracking = await self._handle_ci_fix(pr, evaluation, tracking, report)
            case "wait_for_fix":
                tracking = await self._handle_wait_for_fix(pr, tracking, report)
            case "request_review_fix":
                tracking = await self._handle_review_fix(pr, reviews, tracking, report)
            case "request_review_approve":
                tracking = await self._handle_review_approve(pr, tracking, report)
            case "wait":
                report.waiting.append(pr.number)
            case "none":
                pass  # closed/merged, nothing to do
            case _:
                report.waiting.append(pr.number)

        # Handle ownership lifecycle: claim, release, readiness check publishing
        # This runs after the main action to ensure we have the latest evaluation
        tracking = await self._handle_ownership(pr, tracking, evaluation, report)

        return tracking

    async def _handle_approve_workflows(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Approve any workflow runs that require approval."""
        approved_any = False
        for run in evaluation.ci.action_required_runs:
            match = re.search(r"/actions/runs/(\d+)", run.html_url)
            if match:
                run_id = int(match.group(1))
                try:
                    success = await self._github.approve_workflow_run(
                        self._owner, self._repo, run_id
                    )
                    if success:
                        logger.info(
                            "PR #%d: Approved workflow run %d for check run %s",
                            pr.number,
                            run_id,
                            run.name,
                        )
                        approved_any = True
                        _mark_caretaker_touched(tracking)
                    else:
                        message = f"PR #{pr.number}: Failed to approve workflow run {run_id}"
                        logger.warning(message)
                        report.errors.append(message)
                except GitHubAPIError as e:
                    message = f"PR #{pr.number}: Error approving workflow run {run_id}: {e}"
                    logger.error(message)
                    report.errors.append(message)
            else:
                logger.warning(
                    "PR #%d: Could not extract workflow run ID from %s", pr.number, run.html_url
                )

        if approved_any:
            # We approved at least one workflow, meaning CI will resume/restart
            tracking.state = PRTrackingState.CI_PENDING
        else:
            # We couldn't approve any, so just wait
            tracking.state = PRTrackingState.CI_PENDING
            report.waiting.append(pr.number)

        return tracking

    async def _handle_merge(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Attempt to merge a PR that's ready."""
        merge_decision = evaluate_merge(pr, evaluation.ci, evaluation.reviews, self._config)

        if merge_decision.should_merge:
            try:
                success = await self._github.merge_pull_request(
                    self._owner,
                    self._repo,
                    pr.number,
                    method=merge_decision.method,
                )
            except GitHubAPIError as exc:
                # 405 = branch-protection rules not met (missing review/status check)
                # 409 = merge conflict
                # 422 = unprocessable (e.g. head ref out of date)
                if exc.status_code in (405, 409, 422):
                    logger.warning(
                        "PR #%d cannot be merged yet (HTTP %d): %s",
                        pr.number,
                        exc.status_code,
                        exc,
                    )
                    report.waiting.append(pr.number)
                    return tracking
                raise
            if success:
                logger.info("PR #%d merged via %s", pr.number, merge_decision.method)
                tracking.state = PRTrackingState.MERGED
                tracking.merged_at = datetime.now(UTC)
                # Attribution: caretaker's merge authority closed this PR.
                # `caretaker_merged` implies `caretaker_touched`; we set
                # both through the shared helper so the
                # ``last_caretaker_action_at`` cutoff moves forward.
                _mark_caretaker_touched(tracking)
                tracking.caretaker_merged = True
                report.merged.append(pr.number)
            else:
                logger.warning("PR #%d merge failed", pr.number)
                report.errors.append(f"PR #{pr.number}: merge failed")
        else:
            logger.info(
                "PR #%d not eligible for auto-merge: %s",
                pr.number,
                merge_decision.reason,
            )
            # E2 diagnosis: when a PR is approved but still blocked, emit a
            # structured snapshot so the next occurrence (portfolio #151-class
            # — approved + Copilot pushed a new commit post-approval) can be
            # root-caused from logs rather than manual GitHub archaeology.
            if evaluation.reviews.approved:
                logger.info(
                    "PR #%d merge-block diagnosis: blockers=%s ci_status=%s "
                    "changes_requested=%s approving_reviewers=%s automated_comments=%d "
                    "draft=%s mergeable=%s copilot_pr=%s",
                    pr.number,
                    merge_decision.blockers,
                    evaluation.ci.status.value,
                    evaluation.reviews.changes_requested,
                    [r.user.login for r in evaluation.reviews.approving_reviews],
                    len(evaluation.reviews.automated_review_comments),
                    pr.draft,
                    pr.mergeable,
                    pr.is_copilot_pr,
                )
            report.waiting.append(pr.number)

        return tracking

    async def _has_pending_task_comment(self, pr_number: int) -> bool:
        """Check if there's already a pending (unanswered) task comment on the PR.

        Returns True when the most recent ``caretaker:task`` comment has **no**
        subsequent ``caretaker:result`` comment — meaning a fix request is
        already outstanding and we should not spam another one.
        """
        comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)

        # ``get_pr_comments`` does not guarantee any ordering. Sort explicitly
        # by (created_at, id) so the "last task before any result" logic is
        # stable regardless of how the API returned the page. ``created_at`` is
        # required on Comment; ``id`` tie-breaks same-timestamp entries.
        ordered = sorted(comments, key=lambda c: (c.created_at, c.id))

        last_task_idx: int | None = None
        for i, comment in enumerate(ordered):
            if comment.is_maintainer_task:
                last_task_idx = i

        if last_task_idx is None:
            return False

        # Check if any result comment exists after the last task
        return all(not comment.is_maintainer_result for comment in ordered[last_task_idx + 1 :])

    async def _handle_ci_fix(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle CI failure — request fix from Copilot."""
        # For Copilot/maintainer PRs, skip flaky-retry to request a fix immediately.
        # For human PRs, do one silent wait cycle to guard against transient flakes.
        is_automated_pr = pr.is_copilot_pr or pr.is_maintainer_pr
        if (
            not is_automated_pr
            and tracking.ci_attempts < self._config.ci.flaky_retries
            and evaluation.ci.failed_runs
        ):
            tracking.ci_attempts += 1
            logger.info(
                "PR #%d: retrying CI (flaky retry %d/%d)",
                pr.number,
                tracking.ci_attempts,
                self._config.ci.flaky_retries,
            )
            report.waiting.append(pr.number)
            return tracking

        # E3: when the prior attempt is older than retry_window_hours, reset
        # the attempt counter — old failures shouldn't compound to escalation
        # on long-lived PRs that genuinely needed time.
        window_h = self._config.copilot.retry_window_hours
        if (
            window_h > 0
            and tracking.copilot_attempts > 0
            and tracking.last_copilot_attempt_at is not None
        ):
            last = tracking.last_copilot_attempt_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            age_h = (datetime.now(UTC) - last).total_seconds() / 3600.0
            if age_h >= window_h:
                logger.info(
                    "PR #%d: resetting copilot_attempts (last attempt %.1fh ago, "
                    "outside %dh retry window)",
                    pr.number,
                    age_h,
                    window_h,
                )
                tracking.copilot_attempts = 0

        # Check if we've exceeded Copilot retry limit
        if tracking.copilot_attempts >= self._config.copilot.max_retries:
            logger.warning(
                "PR #%d: max Copilot retries reached (%d), escalating",
                pr.number,
                tracking.copilot_attempts,
            )
            await self._escalate(
                pr,
                "Max CI fix retries exceeded",
                debug_data={
                    "copilot_attempts": tracking.copilot_attempts,
                    "max_retries": self._config.copilot.max_retries,
                    "failed_runs": [run.name for run in evaluation.ci.failed_runs],
                },
            )
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            _mark_caretaker_touched(tracking)
            report.escalated.append(pr.number)
            return tracking

        # Guard against duplicate task comments (e.g. concurrent workflow runs
        # that all loaded the same stale persisted state).
        if await self._has_pending_task_comment(pr.number):
            logger.info(
                "PR #%d: pending task comment already exists — skipping duplicate",
                pr.number,
            )
            tracking.state = PRTrackingState.FIX_REQUESTED
            report.waiting.append(pr.number)
            return tracking

        # Triage the failure and request a fix
        for failed_run in evaluation.ci.failed_runs[:1]:  # Fix one at a time
            triage = await triage_failure(failed_run, self._llm)

            if triage.failure_type == FailureType.BACKLOG:
                return await self._handle_ci_backlog(pr, tracking, report)

            # Refuse to ask Copilot to fix nothing. When the upstream check_run
            # produced no usable error output, posting a TASK comment with an
            # empty error block has historically led to Copilot opening
            # "[WIP] Fix CI failure (unknown)" PRs that get auto-closed.
            # Wait for the next cycle when logs may have been captured.
            if (
                triage.failure_type == FailureType.UNKNOWN
                and not (triage.error_summary or "").strip()
                and not (triage.raw_output or "").strip()
            ):
                logger.info(
                    "PR #%d: skipping @copilot task — unknown failure with empty logs (job=%s)",
                    pr.number,
                    triage.job_name,
                )
                tracking.notes = "skipped_empty_unknown_failure"
                report.waiting.append(pr.number)
                return tracking

            attempt = tracking.copilot_attempts + 1

            stuck_analysis = await self._maybe_analyze_stuck_pr(pr, tracking, triage.error_summary)
            if stuck_analysis:
                tracking.stuck_reflection_done = True

            result = await self._copilot_bridge.request_ci_fix(
                pr=pr,
                triage=triage,
                attempt=attempt,
                issue_context=stuck_analysis if stuck_analysis else "",
            )

            tracking.copilot_attempts = attempt
            tracking.last_task_comment_id = result.comment_id
            tracking.state = PRTrackingState.FIX_REQUESTED
            tracking.last_state_change_at = datetime.now(UTC)
            tracking.last_copilot_attempt_at = datetime.now(UTC)
            _mark_caretaker_touched(tracking)
            report.fix_requested.append(pr.number)
            logger.info(
                "PR #%d: CI fix requested (attempt %d/%d)",
                pr.number,
                attempt,
                self._config.copilot.max_retries,
            )

        return tracking

    async def _maybe_analyze_stuck_pr(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        error_summary: str,
    ) -> str:
        """Return a stuck-PR analysis string when the fix_cycles threshold is met."""
        if tracking.fix_cycles < 2 or tracking.stuck_reflection_done or not self._llm:
            return ""
        if not self._llm.feature_enabled("ci_log_analysis"):
            return ""
        skill_hints = ""
        if self._insight_store is not None:
            skills = self._insight_store.get_relevant("ci", error_summary)
            if skills:
                skill_hints = "\n".join(
                    f"- {s.sop_text} (confidence {s.confidence:.0%})" for s in skills[:3]
                )
        analysis = await self._llm.claude.analyze_stuck_pr(
            pr_number=pr.number,
            previous_attempts=tracking.copilot_attempts,
            ci_log=error_summary,
            known_skills=skill_hints,
        )
        logger.info(
            "PR #%d: stuck analysis generated (fix_cycles=%d)", pr.number, tracking.fix_cycles
        )
        return analysis

    async def _handle_ci_backlog(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle a deliberate CI failure caused by repository queue pressure."""
        should_close_managed_pr = self._config.ci.close_managed_prs_on_backlog and (
            pr.is_copilot_pr or pr.is_maintainer_pr
        )

        if should_close_managed_pr:
            body = (
                "🧹 **Caretaker cleanup**\n\n"
                "Closing this caretaker-managed PR because the repository PR CI backlog "
                "limit was exceeded. This run failed intentionally to reduce queue pressure, "
                "not because caretaker found a code defect. Reopen or regenerate the PR "
                "after the backlog clears."
            )
            await self._github.add_issue_comment(self._owner, self._repo, pr.number, body)
            await self._github.update_issue(self._owner, self._repo, pr.number, state="closed")
            tracking.state = PRTrackingState.CLOSED
            tracking.notes = "closed:ci_backlog_guard"
            _mark_caretaker_touched(tracking)
            logger.info("PR #%d: closed due to CI backlog guard", pr.number)
            return tracking

        logger.info("PR #%d: CI backlog guard tripped; leaving PR open for later retry", pr.number)
        tracking.notes = "ci_backlog_guard"
        report.waiting.append(pr.number)
        return tracking

    async def _handle_wait_for_fix(
        self, pr: PullRequest, tracking: TrackedPR, report: PRAgentReport
    ) -> TrackedPR:
        """Check if Copilot has responded to our fix request."""
        result = await self._copilot_bridge.check_copilot_response(
            pr.number, tracking.last_task_comment_id
        )

        if result is None:
            # No response yet — might have pushed silently, CI will re-evaluate
            report.waiting.append(pr.number)
        elif result.status == ResultStatus.FIXED:
            logger.info("PR #%d: Copilot reports fix applied", pr.number)
            tracking.state = PRTrackingState.CI_PENDING  # Wait for CI re-run
            report.waiting.append(pr.number)
        elif result.status == ResultStatus.BLOCKED:
            logger.warning("PR #%d: Copilot blocked — %s", pr.number, result.blocker)
            await self._escalate(
                pr,
                f"Copilot blocked: {result.blocker}",
                debug_data={
                    "blocker": result.blocker,
                    "copilot_attempts": tracking.copilot_attempts,
                    "last_task_comment_id": tracking.last_task_comment_id,
                },
            )
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            _mark_caretaker_touched(tracking)
            report.escalated.append(pr.number)
        else:
            report.waiting.append(pr.number)

        return tracking

    async def _handle_review_approve(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Submit an approving review for caretaker-owned PRs.

        Called when CI is green, no CHANGES_REQUESTED reviews exist, and the
        PR has not yet been approved.  The caretaker GitHub App identity is
        different from the PR author, so its APPROVE satisfies the
        required-review branch-protection gate.

        Idempotency: ``tracking.last_approved_sha`` records the head SHA of
        the most recent successful auto-approval. Re-entry on the same SHA
        (concurrent webhook + scheduled runs, replayed events, or
        state-machine churn) short-circuits to MERGE_READY without
        submitting a duplicate review — which is what produced the
        "multiple reviews per PR" symptom in the field. A new commit
        advances ``pr.head_sha`` and re-arms the gate naturally.

        Defensive guard: refuse to approve PRs that aren't caretaker-owned
        even if a state-machine bug routed us here. ``is_caretaker_pr``
        keys off the ``claude/`` / ``caretaker/`` head-branch prefix.
        """
        if not getattr(pr, "is_caretaker_pr", False):
            logger.warning(
                "PR #%d: refusing auto-approve — not a caretaker PR (head_ref=%r)",
                pr.number,
                getattr(pr, "head_ref", ""),
            )
            report.waiting.append(pr.number)
            return tracking
        if not self._config.review.auto_approve_caretaker_prs:
            report.waiting.append(pr.number)
            return tracking
        if tracking.last_approved_sha and tracking.last_approved_sha == pr.head_sha:
            logger.info(
                "PR #%d: auto-approve idempotency — head SHA %s already approved, skipping",
                pr.number,
                pr.head_sha[:8] if pr.head_sha else "?",
            )
            tracking.state = PRTrackingState.MERGE_READY
            report.approved.append(pr.number)
            return tracking
        try:
            await self._github.create_review(
                self._owner,
                self._repo,
                pr.number,
                pr.head_sha,
                body="CI green, no blocking review findings — auto-approving caretaker PR.",
                event="APPROVE",
            )
            logger.info("PR #%d: submitted auto-approval", pr.number)
            tracking.state = PRTrackingState.MERGE_READY
            tracking.last_approved_sha = pr.head_sha
            _mark_caretaker_touched(tracking)
            report.approved.append(pr.number)
        except Exception as exc:
            logger.warning("PR #%d: auto-approve failed: %s", pr.number, exc)
            report.errors.append(f"PR #{pr.number}: auto-approve failed: {exc}")
            report.waiting.append(pr.number)
        return tracking

    async def _handle_review_close(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        report: PRAgentReport,
        reason: str,
    ) -> TrackedPR:
        """Close a caretaker PR that a reviewer flagged as infeasible or duplicate.

        Posts a closing comment explaining why the PR is being closed, then
        sets the PR state to closed.  Non-fatal: a failure leaves the PR open
        and adds to report.errors so the operator can investigate.
        """
        # Reason comes from review-summary text which may include embedded
        # newlines; preserve them in a single line so the markdown
        # blockquote (`> {reason}`) renders as one logical quote instead
        # of fragmenting into multiple sibling blocks.
        sanitized_reason = " ".join(reason.split()).strip() or "no reason provided"
        body = (
            "<!-- caretaker:review-close -->\n\n"
            "🔴 **Caretaker: Closing PR**\n\n"
            "A reviewer indicated this change is not viable:\n\n"
            f"> {sanitized_reason}\n\n"
            "Closing to keep the repository clean. "
            "If this was flagged in error, reopen with a comment explaining why "
            "the concern does not apply."
        )
        try:
            await self._github.add_issue_comment(self._owner, self._repo, pr.number, body)
            await self._github.update_issue(self._owner, self._repo, pr.number, state="closed")
            tracking.state = PRTrackingState.CLOSED
            tracking.notes = f"closed:infeasible_review:{reason[:100]}"
            _mark_caretaker_touched(tracking)
            logger.info("PR #%d: closed due to infeasible review: %s", pr.number, reason)
            report.closed.append(pr.number)
        except Exception as exc:
            logger.warning("PR #%d: failed to close infeasible PR: %s", pr.number, exc)
            report.errors.append(f"PR #{pr.number}: close failed: {exc}")
        return tracking

    async def _handle_review_fix(
        self,
        pr: PullRequest,
        reviews: list[Any],
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle review comments — request fixes from Copilot."""
        analyses = await analyze_reviews(
            reviews,
            nitpick_threshold=self._config.review.nitpick_threshold,
            llm_router=self._llm,
            pr_title=pr.title or "",
            repo_slug=f"{self._owner}/{self._repo}",
        )

        if not analyses:
            report.waiting.append(pr.number)
            return tracking

        # Before dispatching a fix, assess whether the review signals something
        # that cannot be fixed mechanically (infeasible / too large / architectural).
        if self._config.review.close_on_infeasible_review:
            verdict, reason = assess_review_verdict(
                analyses,
                pr_additions=getattr(pr, "additions", 0) or 0,
                high_loc_threshold=self._config.review.high_loc_escalate_threshold,
            )
            if verdict == ReviewVerdict.CLOSE:
                return await self._handle_review_close(pr, tracking, report, reason)
            if verdict == ReviewVerdict.ESCALATE:
                await self._escalate(
                    pr,
                    reason,
                    debug_data={
                        "verdict": str(verdict),
                        "review_count": len(analyses),
                        "pr_additions": getattr(pr, "additions", 0),
                    },
                )
                tracking.state = PRTrackingState.ESCALATED
                tracking.escalated = True
                _mark_caretaker_touched(tracking)
                report.escalated.append(pr.number)
                return tracking
            # verdict == FIX or APPROVE — fall through to Copilot dispatch

        attempt = tracking.copilot_attempts + 1
        if attempt > self._config.copilot.max_retries:
            await self._escalate(
                pr,
                "Max review fix retries exceeded",
                debug_data={
                    "copilot_attempts": tracking.copilot_attempts,
                    "max_retries": self._config.copilot.max_retries,
                    "review_count": len(reviews),
                },
            )
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            _mark_caretaker_touched(tracking)
            report.escalated.append(pr.number)
            return tracking

        # Guard against duplicate task comments from concurrent workflow runs.
        if await self._has_pending_task_comment(pr.number):
            logger.info(
                "PR #%d: pending task comment already exists — skipping duplicate review fix",
                pr.number,
            )
            tracking.state = PRTrackingState.FIX_REQUESTED
            report.waiting.append(pr.number)
            return tracking

        result = await self._copilot_bridge.request_review_fix(
            pr=pr, analyses=analyses, attempt=attempt
        )
        tracking.copilot_attempts = attempt
        tracking.last_task_comment_id = result.comment_id
        tracking.last_copilot_attempt_at = datetime.now(UTC)
        tracking.state = PRTrackingState.FIX_REQUESTED
        _mark_caretaker_touched(tracking)
        report.fix_requested.append(pr.number)

        return tracking

    async def _escalate(
        self,
        pr: PullRequest,
        reason: str,
        *,
        debug_data: dict[str, Any] | None = None,
    ) -> None:
        """Escalate a PR to the repo owner."""
        labels = ["maintainer:escalated"]
        await self._github.add_labels(self._owner, self._repo, pr.number, labels)
        payload: dict[str, Any] = {
            "type": "pr_escalation",
            "owner": self._owner,
            "repo": self._repo,
            "pull_request": {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "is_copilot_pr": pr.is_copilot_pr,
                "is_maintainer_pr": pr.is_maintainer_pr,
                "mergeable": pr.mergeable,
                "draft": pr.draft,
                "html_url": pr.html_url,
            },
            "reason": reason,
        }
        if debug_data:
            payload["debug"] = debug_data

        marker = "<!-- caretaker:escalation -->"
        causal = make_causal_marker(
            "pr-agent:escalation",
            parent=parent_from_body(getattr(pr, "body", "") or ""),
        )
        body = (
            f"{marker}\n"
            f"{causal}\n\n"
            f"⚠️ **Caretaker Escalation**\n\n"
            f"This PR requires human attention.\n\n"
            f"**Reason:** {reason}\n\n"
            f"The automated system has exhausted its ability to resolve this. "
            f"Please review and take appropriate action."
        )
        body += render_debug_dump(payload, title="Escalation debug dump")
        # Upsert: one escalation comment per PR, edited in place if the reason
        # changes. Without this, repeated escalation evaluations would spam
        # the PR with identical comments (portfolio #148 saw 14 dupes).
        # Cooldown: don't re-edit the comment more than once per hour even
        # if the body content shifts slightly — a fresh ping every cycle is
        # not what a human reviewer wants.
        await self._github.upsert_issue_comment(
            self._owner,
            self._repo,
            pr.number,
            marker,
            body,
            min_seconds_between_updates=3600,
        )
        logger.info("PR #%d escalated: %s", pr.number, reason)

    async def _attach_readiness_verdict(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        check_runs: list[Any],
        reviews: list[Any],
    ) -> None:
        """Attach a structured :class:`Readiness` verdict to ``evaluation``.

        Runs under the ``@shadow_decision("readiness")`` gate so the
        behaviour across ``off`` / ``shadow`` / ``enforce`` stays uniform
        with every other Phase 2 decision site. When no legacy
        :class:`ReadinessEvaluation` is present (defensive — should never
        happen in the normal flow) the verdict is left ``None``.
        """
        legacy_eval: ReadinessEvaluation | None = evaluation.readiness
        if legacy_eval is None:
            evaluation.readiness_verdict = None
            return

        async def _legacy_path() -> Readiness:
            return readiness_from_legacy(legacy_eval)

        async def _candidate_path(
            *,
            model: str | None = None,
            max_tokens: int | None = None,
        ) -> Readiness | None:
            # ``model`` / ``max_tokens`` are injected by the
            # @shadow_decision decorator when
            # ``agentic.readiness.model_override`` is set (see
            # :func:`caretaker.evolution.shadow._resolve_model_overrides`);
            # both stay ``None`` in the default case so the call shape
            # is identical to pre-override code.
            if self._llm is None or not self._llm.available:
                return None
            context = PRReadinessContext(
                pr=pr,
                check_runs=list(check_runs),
                reviews=list(reviews),
                repo_slug=f"{self._owner}/{self._repo}",
                is_solo_maintainer=self._config.readiness.required_reviews == 0,
            )
            return await evaluate_pr_readiness_llm(
                context,
                claude=self._llm.claude,
                retriever=self._memory_retriever,
                model=model,
                max_tokens=max_tokens,
            )

        try:
            verdict: Readiness = await _decide_readiness(
                legacy=_legacy_path,
                candidate=_candidate_path,
                context={
                    "pr_number": pr.number,
                    "repo_slug": f"{self._owner}/{self._repo}",
                    "labels": [label.name for label in pr.labels],
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the agent
            logger.warning(
                "PR #%d: readiness shadow-decision failed (%s: %s); falling back to legacy adapter",
                pr.number,
                type(exc).__name__,
                exc,
            )
            verdict = readiness_from_legacy(legacy_eval)

        evaluation.readiness_verdict = verdict

    async def _publish_readiness_check(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        evaluation: PRStateEvaluation,
    ) -> None:
        """Publish the caretaker/pr-readiness check run on the PR's head SHA.

        The conclusion depends on ``config.merge_authority.mode``:
        - advisory (default): blocked PRs publish "neutral" so the check is
          informational and never triggers branch-protection blocks, even when
          an operator has listed it as a required check.
        - gate_only / gate_and_merge: blocked PRs publish "failure" to actively
          block merges via branch protection (opt-in behaviour).
        """
        if not self._config.readiness.enabled:
            return

        # Guard against optional readiness field
        if evaluation.readiness is None:
            return

        check_name = self._config.readiness.check_name
        head_sha = pr.head_sha
        if not head_sha:
            logger.warning(
                "PR #%d: missing head SHA, skipping %s check publication",
                pr.number,
                check_name,
            )
            return

        # Update readiness tracking from evaluation
        tracking.readiness_score = evaluation.readiness.score
        tracking.readiness_blockers = evaluation.readiness.blockers
        tracking.readiness_summary = evaluation.readiness.summary

        # Find existing check run
        existing_check = await self._github.find_check_run(
            self._owner, self._repo, head_sha, check_name
        )

        # Determine conclusion based on readiness.
        # In advisory mode the check is informational only: publish "neutral"
        # instead of "failure" so GitHub branch protection never treats it as
        # blocking even if an operator adds the check to their required-checks
        # list.  gate_only / gate_and_merge modes keep the hard "failure"
        # because those modes explicitly opt in to using the check as a gate.
        conclusion = evaluation.readiness.conclusion
        check_status = "completed"
        check_conclusion = None
        if conclusion == "success":
            check_conclusion = "success"
        elif conclusion == "failure":
            if self._config.merge_authority.mode == MergeAuthorityMode.ADVISORY:
                check_conclusion = "neutral"
            else:
                check_conclusion = "failure"
        else:  # in_progress
            check_status = "in_progress"

        check_title = get_readiness_check_title(tracking, evaluation.readiness_verdict)
        check_summary = get_readiness_check_summary(tracking, evaluation.readiness_verdict)

        now_iso = datetime.now(UTC).isoformat()
        create_kwargs: dict[str, Any] = dict(
            status=check_status,
            conclusion=check_conclusion,
            output_title=check_title,
            output_summary=check_summary,
            started_at=now_iso,
            completed_at=(now_iso if check_status == "completed" else None),
        )

        try:
            if existing_check:
                # Proactive cross-App ownership check: if we know our own app_id and
                # the existing check was created by a *different* App, skip the update
                # and create a new check run instead.  This avoids a 403 "Invalid
                # app_id" round-trip that GitHub would otherwise return.
                owned_by_us = (
                    self._app_id is None  # identity unknown → try anyway
                    or existing_check.app_id is None  # check has no app metadata → try anyway
                    or existing_check.app_id == self._app_id
                )
                if not owned_by_us:
                    logger.info(
                        "PR #%d: check_run id=%d owned by App %d (we are App %d) — "
                        "creating a new %s check instead of updating",
                        pr.number,
                        existing_check.id,
                        existing_check.app_id,
                        self._app_id,
                        check_name,
                    )
                    result = await self._github.create_check_run(
                        self._owner,
                        self._repo,
                        check_name,
                        head_sha,
                        **create_kwargs,
                    )
                    logger.debug(
                        "PR #%d: Created replacement %s check (id=%s)",
                        pr.number,
                        check_name,
                        result.get("id"),
                    )
                else:
                    # Update existing check run (we own it or identity is unknown)
                    try:
                        await self._github.update_check_run(
                            self._owner,
                            self._repo,
                            existing_check.id,
                            status=check_status,
                            conclusion=check_conclusion,
                            output_title=check_title,
                            output_summary=check_summary,
                            completed_at=(now_iso if check_status == "completed" else None),
                        )
                        logger.debug(
                            "PR #%d: Updated %s check (id=%d, conclusion=%s)",
                            pr.number,
                            check_name,
                            existing_check.id,
                            check_conclusion,
                        )
                    except GitHubAPIError as update_err:
                        # Secondary safety net: GitHub returns 403 "Invalid app_id"
                        # when ownership check above couldn't detect a conflict (e.g.
                        # app_id was missing from the check run metadata).  Fall back
                        # to creating a new check run so readiness is always published.
                        if (
                            update_err.status_code == 403
                            and "invalid app_id" in str(update_err).lower()
                        ):
                            logger.info(
                                "PR #%d: check_run id=%d rejected with app_id mismatch — "
                                "falling back to create a new %s check",
                                pr.number,
                                existing_check.id,
                                check_name,
                            )
                            result = await self._github.create_check_run(
                                self._owner,
                                self._repo,
                                check_name,
                                head_sha,
                                **create_kwargs,
                            )
                            logger.debug(
                                "PR #%d: Created replacement %s check (id=%s)",
                                pr.number,
                                check_name,
                                result.get("id"),
                            )
                        else:
                            raise
            else:
                # Create new check run
                result = await self._github.create_check_run(
                    self._owner,
                    self._repo,
                    check_name,
                    head_sha,
                    **create_kwargs,
                )
                logger.debug(
                    "PR #%d: Created %s check (id=%s)",
                    pr.number,
                    check_name,
                    result.get("id"),
                )
        except GitHubAPIError as e:
            logger.warning(
                "PR #%d: Failed to publish readiness check: %s",
                pr.number,
                e,
            )

    async def _handle_ownership(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        evaluation: PRStateEvaluation,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle PR ownership — claim, release, and update readiness.

        This method:
        1. Updates readiness tracking from the evaluation
        2. Attempts to claim ownership if eligible
        3. Releases ownership if the PR is merged/closed/escalated
        4. Publishes the readiness check
        5. Edits the single caretaker status comment in place
        """
        # Guard against optional readiness field
        if evaluation.readiness is None:
            return tracking

        # Update readiness tracking
        tracking.readiness_score = evaluation.readiness.score
        tracking.readiness_blockers = evaluation.readiness.blockers
        tracking.readiness_summary = evaluation.readiness.summary

        # Handle ownership state transitions. Each branch that calls out
        # to GitHub counts as a write action for attribution telemetry.
        if should_release_ownership(pr, tracking, "merged"):
            await release_ownership(
                self._github,
                self._owner,
                self._repo,
                pr,
                tracking,
                self._config.ownership,
                reason="PR merged",
            )
            _mark_caretaker_touched(tracking)
        elif should_release_ownership(pr, tracking, "closed"):
            await release_ownership(
                self._github,
                self._owner,
                self._repo,
                pr,
                tracking,
                self._config.ownership,
                reason="PR closed",
            )
            _mark_caretaker_touched(tracking)
        elif should_release_ownership(pr, tracking, "escalated"):
            await release_ownership(
                self._github,
                self._owner,
                self._repo,
                pr,
                tracking,
                self._config.ownership,
                reason="PR escalated",
            )
            _mark_caretaker_touched(tracking)
        elif tracking.ownership_state == OwnershipState.UNOWNED:
            # Try to claim ownership
            claim = await claim_ownership(
                self._github,
                self._owner,
                self._repo,
                pr,
                tracking,
                self._config.ownership,
            )
            if claim.claimed:
                _mark_caretaker_touched(tracking)

        # Publish readiness check
        await self._publish_readiness_check(pr, tracking, evaluation)

        # Edit the single caretaker status comment in place for owned, still-open
        # PRs. This keeps one living comment that transitions through
        # ⏳ monitoring → ✅ ready for merge, rather than appending a new comment
        # on every evaluation. release_ownership handles the terminal body
        # (merged / closed / escalated) when the PR reaches a final state.
        is_terminal = bool(pr.merged) or getattr(pr.state, "value", pr.state) == "closed"
        if tracking.ownership_state == OwnershipState.OWNED and not is_terminal:
            try:
                await upsert_status_comment(
                    self._github,
                    self._owner,
                    self._repo,
                    pr.number,
                    build_status_comment(
                        pr, tracking, readiness_verdict=evaluation.readiness_verdict
                    ),
                )
                _mark_caretaker_touched(tracking)
            except GitHubAPIError as e:
                logger.warning(
                    "PR #%d: Failed to upsert caretaker status comment: %s",
                    pr.number,
                    e,
                )

            # One-shot cleanup of pre-#403 legacy duplicate comments. Idempotent
            # via the tracking flag so we never loop on the same PR.
            if not tracking.legacy_comments_compacted:
                try:
                    removed = await compact_legacy_comments(
                        self._github, self._owner, self._repo, pr.number
                    )
                    if removed:
                        logger.info(
                            "PR #%d: compacted %d legacy caretaker comment(s)",
                            pr.number,
                            removed,
                        )
                except GitHubAPIError as e:
                    logger.warning(
                        "PR #%d: legacy comment compaction failed: %s",
                        pr.number,
                        e,
                    )
                # Mark compacted regardless of removal count: 0 means nothing
                # to compact (good); errors are logged but shouldn't loop.
                tracking.legacy_comments_compacted = True

        return tracking
