"""PR Agent — the main PR monitoring and management agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from project_maintainer.config import PRAgentConfig
from project_maintainer.github_client.api import GitHubClient
from project_maintainer.github_client.models import PullRequest
from project_maintainer.llm.copilot import CopilotProtocol, ResultStatus
from project_maintainer.llm.router import LLMRouter
from project_maintainer.pr_agent.ci_triage import triage_failure
from project_maintainer.pr_agent.copilot import PRCopilotBridge
from project_maintainer.pr_agent.merge import evaluate_merge
from project_maintainer.pr_agent.review import analyze_reviews
from project_maintainer.pr_agent.states import (
    CIStatus,
    PRStateEvaluation,
    evaluate_pr,
)
from project_maintainer.state.models import PRTrackingState, TrackedPR

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
        self, tracked_prs: dict[int, TrackedPR]
    ) -> tuple[PRAgentReport, dict[int, TrackedPR]]:
        """Run the PR agent — evaluate all open PRs and take action."""
        report = PRAgentReport()

        # Discover open PRs
        open_prs = await self._github.list_pull_requests(self._owner, self._repo)
        report.monitored = len(open_prs)

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
        # Fetch CI status and reviews
        check_runs = await self._github.get_check_runs(
            self._owner, self._repo, pr.head_ref
        )
        reviews = await self._github.get_pr_reviews(
            self._owner, self._repo, pr.number
        )

        # Evaluate PR state
        evaluation = evaluate_pr(
            pr=pr,
            check_runs=check_runs,
            reviews=reviews,
            current_state=tracking.state,
            ignore_jobs=self._config.ci.ignore_jobs,
        )

        logger.info(
            "PR #%d: %s → %s (action: %s)",
            pr.number,
            tracking.state.value,
            evaluation.recommended_state.value,
            evaluation.recommended_action,
        )

        # Act on the recommendation
        match evaluation.recommended_action:
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

        tracking.state = evaluation.recommended_state
        return tracking

    async def _handle_merge(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Attempt to merge a PR that's ready."""
        merge_decision = evaluate_merge(
            pr, evaluation.ci, evaluation.reviews, self._config
        )

        if merge_decision.should_merge:
            success = await self._github.merge_pull_request(
                self._owner,
                self._repo,
                pr.number,
                method=merge_decision.method,
            )
            if success:
                logger.info("PR #%d merged via %s", pr.number, merge_decision.method)
                tracking.state = PRTrackingState.MERGED
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

    async def _handle_ci_fix(
        self,
        pr: PullRequest,
        evaluation: PRStateEvaluation,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle CI failure — request fix from Copilot."""
        # Check if we should retry CI first (flaky test handling)
        if (
            tracking.ci_attempts < self._config.ci.flaky_retries
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
            await self._escalate(pr, "Max CI fix retries exceeded")
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            report.escalated.append(pr.number)
            return tracking

        # Triage the failure and request a fix
        for failed_run in evaluation.ci.failed_runs[:1]:  # Fix one at a time
            triage = await triage_failure(failed_run, self._llm)
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
            await self._escalate(pr, f"Copilot blocked: {result.blocker}")
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            report.escalated.append(pr.number)
        else:
            report.waiting.append(pr.number)

        return tracking

    async def _handle_review_fix(
        self,
        pr: PullRequest,
        reviews: list,
        tracking: TrackedPR,
        report: PRAgentReport,
    ) -> TrackedPR:
        """Handle review comments — request fixes from Copilot."""
        if not pr.is_copilot_pr:
            # Don't auto-fix non-Copilot PRs
            report.waiting.append(pr.number)
            return tracking

        from project_maintainer.github_client.models import Review

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
            await self._escalate(pr, "Max review fix retries exceeded")
            tracking.state = PRTrackingState.ESCALATED
            tracking.escalated = True
            report.escalated.append(pr.number)
            return tracking

        result = await self._copilot_bridge.request_review_fix(
            pr=pr, analyses=analyses, attempt=attempt
        )
        tracking.copilot_attempts = attempt
        tracking.last_task_comment_id = result.comment_id
        tracking.state = PRTrackingState.FIX_REQUESTED
        report.fix_requested.append(pr.number)

        return tracking

    async def _escalate(self, pr: PullRequest, reason: str) -> None:
        """Escalate a PR to the repo owner."""
        labels = ["maintainer:escalated"]
        await self._github.add_labels(
            self._owner, self._repo, pr.number, labels
        )
        body = (
            f"⚠️ **Project Maintainer Escalation**\n\n"
            f"This PR requires human attention.\n\n"
            f"**Reason:** {reason}\n\n"
            f"The automated system has exhausted its ability to resolve this. "
            f"Please review and take appropriate action."
        )
        await self._github.add_issue_comment(
            self._owner, self._repo, pr.number, body
        )
        logger.info("PR #%d escalated: %s", pr.number, reason)
