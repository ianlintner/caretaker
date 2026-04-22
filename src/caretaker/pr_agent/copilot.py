"""Copilot interaction protocol for the PR Agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.evolution.insight_store import CATEGORY_CI
from caretaker.llm.copilot import (
    CopilotProtocol,
    CopilotResult,
    CopilotTask,
    TaskType,
)
from caretaker.pr_agent.ci_triage import FailureType

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.foundry.dispatcher import ExecutorDispatcher, RouteResult
    from caretaker.github_client.models import PullRequest
    from caretaker.pr_agent.ci_triage import TriageResult
    from caretaker.pr_agent.review import ReviewAnalysis

# Map FailureType values to the closest TaskType; unmapped types fall back to CI_FAILURE.
_FAILURE_TYPE_TO_TASK_TYPE: dict[FailureType, TaskType] = {
    FailureType.TEST_FAILURE: TaskType.TEST_FAILURE,
    FailureType.LINT_FAILURE: TaskType.LINT_FAILURE,
    FailureType.BUILD_FAILURE: TaskType.BUILD_FAILURE,
    FailureType.TYPE_ERROR: TaskType.BUILD_FAILURE,
    FailureType.TIMEOUT: TaskType.CI_FAILURE,
    FailureType.BACKLOG: TaskType.CI_FAILURE,
    FailureType.UNKNOWN: TaskType.CI_FAILURE,
}

logger = logging.getLogger(__name__)


@dataclass
class CopilotInteractionResult:
    task_posted: bool
    task_type: str
    attempt: int
    max_attempts: int
    comment_id: int | None = None
    # Populated when the ExecutorDispatcher routed to Foundry (fully or as a
    # pre-Copilot attempt). Left None when the bridge fell through the legacy
    # path with no dispatcher configured.
    route: RouteResult | None = None


class PRCopilotBridge:
    """Bridge between PR Agent decisions and Copilot / Foundry execution.

    Historically this only posted ``@copilot`` comments.  It now also accepts
    an optional :class:`caretaker.foundry.dispatcher.ExecutorDispatcher`: when
    present, tasks are routed through the dispatcher (which may run them via
    the Foundry in-process executor, or fall back to Copilot).  When
    ``dispatcher`` is ``None`` the behavior is byte-identical to before.
    """

    def __init__(
        self,
        protocol: CopilotProtocol,
        max_retries: int = 2,
        insight_store: InsightStore | None = None,
        dispatcher: ExecutorDispatcher | None = None,
    ) -> None:
        self._protocol = protocol
        self._max_retries = max_retries
        self._insight_store = insight_store
        self._dispatcher = dispatcher

    async def request_ci_fix(
        self,
        pr: PullRequest,
        triage: TriageResult,
        attempt: int = 1,
        issue_context: str = "",
    ) -> CopilotInteractionResult:
        """Post a CI fix request via Copilot or the Foundry dispatcher."""
        task = CopilotTask(
            task_type=_FAILURE_TYPE_TO_TASK_TYPE.get(triage.failure_type, TaskType.CI_FAILURE),
            job_name=triage.job_name,
            error_output=triage.error_summary,
            instructions=triage.instructions,
            attempt=attempt,
            max_attempts=self._max_retries,
            priority="high",
            context=issue_context,
        )

        if self._insight_store is not None:
            skills = self._insight_store.get_relevant(CATEGORY_CI, triage.error_summary)
            task.enrich_with_skills(skills)

        return await self._dispatch(
            pr=pr,
            copilot_task=task,
            attempt=attempt,
            task_type_label=triage.failure_type.value,
        )

    async def request_review_fix(
        self,
        pr: PullRequest,
        analyses: list[ReviewAnalysis],
        attempt: int = 1,
    ) -> CopilotInteractionResult:
        """Post review fix instructions via Copilot or the Foundry dispatcher."""
        # Build combined instructions from all blocking reviews. Include
        # the structured severity (from T-A4's ``ReviewClassification``)
        # so the Copilot bridge can rank its work — a ``blocker`` review
        # should be addressed before a ``minor`` one, even when both are
        # on the same PR.
        review_items = []
        for i, analysis in enumerate(analyses, 1):
            severity_tag = f"severity={analysis.severity}"
            review_items.append(
                f"{i}. **{analysis.reviewer}** "
                f"({analysis.comment_type.value}, {severity_tag}): {analysis.summary}"
            )

        instructions = (
            "Address the following review comments:\n"
            + "\n".join(review_items)
            + "\n\nFor each comment:\n"
            "1. Make the requested change\n"
            "2. Ensure tests still pass\n"
            "3. Reply with a RESULT block when all comments are addressed"
        )

        # Blocker severity is the one case where we bump the Copilot
        # priority so the scheduler picks the task up ahead of routine
        # lint fixes. Everything else stays medium (unchanged from
        # pre-T-A4 behaviour).
        priority = "high" if any(a.severity == "blocker" for a in analyses) else "medium"

        task = CopilotTask(
            task_type=TaskType.REVIEW_COMMENT,
            job_name="review",
            error_output="\n".join(review_items),
            instructions=instructions,
            attempt=attempt,
            max_attempts=self._max_retries,
            priority=priority,
        )

        return await self._dispatch(
            pr=pr,
            copilot_task=task,
            attempt=attempt,
            task_type_label="REVIEW_COMMENT",
        )

    async def _dispatch(
        self,
        *,
        pr: PullRequest,
        copilot_task: CopilotTask,
        attempt: int,
        task_type_label: str,
    ) -> CopilotInteractionResult:
        """Shared routing logic for CI and review fix requests.

        When no dispatcher is configured the call path is byte-identical to
        the legacy ``self._protocol.post_task(...)`` dispatch, so existing
        integrations and tests are unaffected.
        """
        if self._dispatcher is None:
            comment = await self._protocol.post_task(pr.number, copilot_task)
            return CopilotInteractionResult(
                task_posted=True,
                task_type=task_type_label,
                attempt=attempt,
                max_attempts=self._max_retries,
                comment_id=comment.id,
            )

        route = await self._dispatcher.route(pr=pr, copilot_task=copilot_task)
        # The state machine stores this as ``last_task_comment_id`` and later
        # polls ``find_latest_result(after_comment_id=...)``. That filter
        # *skips* any comment whose id <= the stored one. So:
        #   - Copilot path → id of the TASK comment; the upcoming RESULT
        #     comment will have a later id and be found. ✓
        #   - Foundry fallback path → same (Copilot task comment was posted).
        #   - Foundry success path → Foundry already pushed a commit AND
        #     posted the RESULT comment. Returning its id here would cause
        #     the next poll to skip it. Leave as None so the poll scans the
        #     full comment list and finds the Foundry result.
        comment_id: int | None = (
            route.copilot_comment.id if route.copilot_comment is not None else None
        )

        return CopilotInteractionResult(
            task_posted=True,
            task_type=task_type_label,
            attempt=attempt,
            max_attempts=self._max_retries,
            comment_id=comment_id,
            route=route,
        )

    async def check_copilot_response(
        self, pr_number: int, after_comment_id: int | None = None
    ) -> CopilotResult | None:
        """Check if Copilot (or Foundry) has responded to our task."""
        return await self._protocol.find_latest_result(pr_number, after_comment_id)

    async def get_attempt_count(self, pr_number: int) -> int:
        """Get how many task attempts have been posted."""
        return await self._protocol.count_task_attempts(pr_number)
