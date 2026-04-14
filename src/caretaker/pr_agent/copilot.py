"""Copilot interaction protocol for the PR Agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from caretaker.llm.copilot import (
    CopilotProtocol,
    CopilotResult,
    CopilotTask,
    TaskType,
)

if TYPE_CHECKING:
    from caretaker.github_client.models import PullRequest
    from caretaker.pr_agent.ci_triage import TriageResult
    from caretaker.pr_agent.review import ReviewAnalysis

logger = logging.getLogger(__name__)


@dataclass
class CopilotInteractionResult:
    task_posted: bool
    task_type: str
    attempt: int
    max_attempts: int
    comment_id: int | None = None


class PRCopilotBridge:
    """Bridge between PR Agent decisions and Copilot execution via comments."""

    def __init__(self, protocol: CopilotProtocol, max_retries: int = 2) -> None:
        self._protocol = protocol
        self._max_retries = max_retries

    async def request_ci_fix(
        self,
        pr: PullRequest,
        triage: TriageResult,
        attempt: int = 1,
        issue_context: str = "",
    ) -> CopilotInteractionResult:
        """Post a CI fix request to Copilot via PR comment."""
        task = CopilotTask(
            task_type=TaskType(triage.failure_type.value),
            job_name=triage.job_name,
            error_output=triage.error_summary,
            instructions=triage.instructions,
            attempt=attempt,
            max_attempts=self._max_retries,
            priority="high",
            context=issue_context,
        )

        comment = await self._protocol.post_task(pr.number, task)
        return CopilotInteractionResult(
            task_posted=True,
            task_type=triage.failure_type.value,
            attempt=attempt,
            max_attempts=self._max_retries,
            comment_id=comment.id,
        )

    async def request_review_fix(
        self,
        pr: PullRequest,
        analyses: list[ReviewAnalysis],
        attempt: int = 1,
    ) -> CopilotInteractionResult:
        """Post review fix instructions to Copilot."""
        # Build combined instructions from all blocking reviews
        review_items = []
        for i, analysis in enumerate(analyses, 1):
            review_items.append(
                f"{i}. **{analysis.reviewer}** ({analysis.comment_type.value}): {analysis.summary}"
            )

        instructions = (
            "Address the following review comments:\n"
            + "\n".join(review_items)
            + "\n\nFor each comment:\n"
            "1. Make the requested change\n"
            "2. Ensure tests still pass\n"
            "3. Reply with a RESULT block when all comments are addressed"
        )

        task = CopilotTask(
            task_type=TaskType.REVIEW_COMMENT,
            job_name="review",
            error_output="\n".join(review_items),
            instructions=instructions,
            attempt=attempt,
            max_attempts=self._max_retries,
            priority="medium",
        )

        comment = await self._protocol.post_task(pr.number, task)
        return CopilotInteractionResult(
            task_posted=True,
            task_type="REVIEW_COMMENT",
            attempt=attempt,
            max_attempts=self._max_retries,
            comment_id=comment.id,
        )

    async def check_copilot_response(
        self, pr_number: int, after_comment_id: int | None = None
    ) -> CopilotResult | None:
        """Check if Copilot has responded to our task."""
        return await self._protocol.find_latest_result(pr_number, after_comment_id)

    async def get_attempt_count(self, pr_number: int) -> int:
        """Get how many task attempts have been posted."""
        return await self._protocol.count_task_attempts(pr_number)
