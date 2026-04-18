"""PR Agent — the main PR monitoring and management agent."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.github_client.api import GitHubAPIError
from caretaker.github_client.models import PRState
from caretaker.llm.copilot import CopilotProtocol, ResultStatus
from caretaker.pr_agent.ci_triage import FailureType, triage_failure
from caretaker.pr_agent.copilot import PRCopilotBridge
from caretaker.pr_agent.merge import evaluate_merge
from caretaker.pr_agent.ownership import (
    build_readiness_comment,
    claim_ownership,
    get_readiness_check_summary,
    get_readiness_check_title,
    release_ownership,
    should_release_ownership,
)
from caretaker.pr_agent.review import analyze_reviews
from caretaker.pr_agent.states import (
    PRStateEvaluation,
    evaluate_pr,
)
from caretaker.state.models import OwnershipState, PRTrackingState, TrackedPR
from caretaker.tools.debug_dump import render_debug_dump

if TYPE_CHECKING:
    from caretaker.config import PRAgentConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)


@dataclass
class PRAgentReport:
    monitored: int = 0
    merged: list[int] = field(default_factory=list)
    escalated: list[int] = field(default_factory=list)
    fix_requested: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class PRAgent:
    """Monitors and manages pull requests through their lifecycle."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: PRAgentConfig,
        llm_router: LLMRouter | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._llm = llm_router
        self._copilot_protocol = CopilotProtocol(github, owner, repo)
        self._copilot_bridge = PRCopilotBridge(
            self._copilot_protocol,
            max_retries=config.copilot.max_retries,
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
            for pr_number, tracked in list(tracked_prs.items()):
                if pr_number not in open_pr_numbers and tracked.state not in _terminal:
                    try:
                        closed_pr = await self._github.get_pull_request(
                            self._owner, self._repo, pr_number
                        )
                        if closed_pr is not None:
                            if closed_pr.merged:
                                tracked.state = PRTrackingState.MERGED
                                # Prefer GitHub's true merge timestamp when we don't already
                                # have one persisted from a prior cycle.
                                if tracked.merged_at is None:
                                    tracked.merged_at = closed_pr.merged_at
                                logger.info(
                                    "PR #%d: externally merged — updated tracked state", pr_number
                                )
                            elif closed_pr.state == PRState.CLOSED:
                                tracked.state = PRTrackingState.CLOSED
                                logger.info(
                                    "PR #%d: externally closed — updated tracked state", pr_number
                                )
                    except Exception as exc:
                        logger.debug("Could not sync state for PR #%d: %s", pr_number, exc)

        for pr in open_prs:
            try:
                tracking = tracked_prs.get(pr.number, TrackedPR(number=pr.number))
                tracking = await self._process_pr(pr, tracking, report)
                tracking.last_checked = datetime.utcnow()
                tracked_prs[pr.number] = tracking
            except Exception as e:
                logger.error("Error processing PR #%d: %s", pr.number, e)
                report.errors.append(f"PR #{pr.number}: {e}")

        return report, tracked_prs

    async def _process_pr(
        self, pr: PullRequest, tracking: TrackedPR, report: PRAgentReport
    ) -> TrackedPR:
        """Process a single PR through the state machine."""
        if tracking.first_seen_at is None:
            tracking.first_seen_at = datetime.utcnow()

        # Fetch CI status and reviews
        check_runs = await self._github.get_check_runs(self._owner, self._repo, pr.head_ref)
        reviews = await self._github.get_pr_reviews(self._owner, self._repo, pr.number)

        # Evaluate PR state
        evaluation = evaluate_pr(
            pr=pr,
            check_runs=check_runs,
            reviews=reviews,
            current_state=tracking.state,
            ignore_jobs=self._config.ci.ignore_jobs,
            auto_approve_workflows=self._config.ci.auto_approve_workflows,
        )

        logger.info(
            "PR #%d: %s → %s (action: %s)",
            pr.number,
            tracking.state.value,
            evaluation.recommended_state.value,
            evaluation.recommended_action,
        )

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
                tracking.merged_at = datetime.utcnow()
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
            report.waiting.append(pr.number)

        return tracking

    async def _has_pending_task_comment(self, pr_number: int) -> bool:
        """Check if there's already a pending (unanswered) task comment on the PR.

        Returns True when the most recent ``caretaker:task`` comment has **no**
        subsequent ``caretaker:result`` comment — meaning a fix request is
        already outstanding and we should not spam another one.
        """
        comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        last_task_idx: int | None = None
        for i, comment in enumerate(comments):
            if comment.is_maintainer_task:
                last_task_idx = i

        if last_task_idx is None:
            return False

        # Check if any result comment exists after the last task
        return all(not comment.is_maintainer_result for comment in comments[last_task_idx + 1 :])

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

            attempt = tracking.copilot_attempts + 1

            result = await self._copilot_bridge.request_ci_fix(
                pr=pr,
                triage=triage,
                attempt=attempt,
            )

            tracking.copilot_attempts = attempt
            tracking.last_task_comment_id = result.comment_id
            tracking.state = PRTrackingState.FIX_REQUESTED
            report.fix_requested.append(pr.number)
            logger.info(
                "PR #%d: CI fix requested (attempt %d/%d)",
                pr.number,
                attempt,
                self._config.copilot.max_retries,
            )

        return tracking

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
            report.escalated.append(pr.number)
        else:
            report.waiting.append(pr.number)

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
        )

        if not analyses:
            report.waiting.append(pr.number)
            return tracking

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
        tracking.state = PRTrackingState.FIX_REQUESTED
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

        body = (
            f"⚠️ **Caretaker Escalation**\n\n"
            f"This PR requires human attention.\n\n"
            f"**Reason:** {reason}\n\n"
            f"The automated system has exhausted its ability to resolve this. "
            f"Please review and take appropriate action."
        )
        body += render_debug_dump(payload, title="Escalation debug dump")
        await self._github.add_issue_comment(self._owner, self._repo, pr.number, body)
        logger.info("PR #%d escalated: %s", pr.number, reason)

    async def _publish_readiness_check(
        self,
        pr: PullRequest,
        tracking: TrackedPR,
        evaluation: PRStateEvaluation,
    ) -> None:
        """Publish the caretaker/pr-readiness check run on the PR's head SHA.

        This publishes a non-required check (Phase 1) that shows PR readiness status.
        The check is updated on every evaluation to keep the status current.
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

        # Determine conclusion based on readiness
        conclusion = evaluation.readiness.conclusion
        check_status = "completed"
        check_conclusion = None
        if conclusion == "success":
            check_conclusion = "success"
        elif conclusion == "failure":
            check_conclusion = "failure"
        else:  # in_progress
            check_status = "in_progress"

        check_title = get_readiness_check_title(tracking)
        check_summary = get_readiness_check_summary(tracking)

        try:
            if existing_check:
                # Update existing check run
                await self._github.update_check_run(
                    self._owner,
                    self._repo,
                    existing_check.id,
                    status=check_status,
                    conclusion=check_conclusion,
                    output_title=check_title,
                    output_summary=check_summary,
                    completed_at=(
                        datetime.now(UTC).isoformat() if check_status == "completed" else None
                    ),
                )
                logger.debug(
                    "PR #%d: Updated %s check (id=%d, conclusion=%s)",
                    pr.number,
                    check_name,
                    existing_check.id,
                    check_conclusion,
                )
            else:
                # Create new check run
                result = await self._github.create_check_run(
                    self._owner,
                    self._repo,
                    check_name,
                    head_sha,
                    status=check_status,
                    conclusion=check_conclusion,
                    output_title=check_title,
                    output_summary=check_summary,
                    started_at=datetime.now(UTC).isoformat(),
                    completed_at=(
                        datetime.now(UTC).isoformat() if check_status == "completed" else None
                    ),
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
        5. Posts update comments on meaningful changes
        """
        previous_score = tracking.readiness_score
        previous_blockers = list(tracking.readiness_blockers)

        # Guard against optional readiness field
        if evaluation.readiness is None:
            return tracking

        # Update readiness tracking
        tracking.readiness_score = evaluation.readiness.score
        tracking.readiness_blockers = evaluation.readiness.blockers
        tracking.readiness_summary = evaluation.readiness.summary

        # Handle ownership state transitions
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
        elif tracking.ownership_state == OwnershipState.UNOWNED:
            # Try to claim ownership
            await claim_ownership(
                self._github,
                self._owner,
                self._repo,
                pr,
                tracking,
                self._config.ownership,
            )

        # Publish readiness check
        await self._publish_readiness_check(pr, tracking, evaluation)

        # Post update comment on meaningful changes (only for owned PRs)
        if tracking.ownership_state == OwnershipState.OWNED:
            update_comment = build_readiness_comment(
                tracking,
                previous_score,
                previous_blockers,
            )
            if update_comment:
                try:
                    await self._github.add_issue_comment(
                        self._owner, self._repo, pr.number, update_comment
                    )
                except GitHubAPIError as e:
                    logger.warning(
                        "PR #%d: Failed to post readiness update comment: %s",
                        pr.number,
                        e,
                    )

        return tracking
