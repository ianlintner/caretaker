"""Copilot interaction via structured GitHub comments."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Comment

logger = logging.getLogger(__name__)

TASK_OPEN = "<!-- caretaker:task -->"
TASK_CLOSE = "<!-- /caretaker:task -->"
RESULT_OPEN = "<!-- caretaker:result -->"
RESULT_CLOSE = "<!-- /caretaker:result -->"


class TaskType(StrEnum):
    CI_FAILURE = "CI_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    REVIEW_COMMENT = "REVIEW_COMMENT"
    REBASE = "REBASE"
    GENERIC = "GENERIC"


class ResultStatus(StrEnum):
    FIXED = "FIXED"
    BLOCKED = "BLOCKED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass
class CopilotTask:
    task_type: TaskType
    job_name: str
    error_output: str
    instructions: str
    attempt: int
    max_attempts: int
    priority: str = "medium"
    context: str = ""

    def to_comment(self) -> str:
        lines = [
            "@copilot",
            "",
            TASK_OPEN,
            f"TASK: Fix {self.task_type.value.replace('_', ' ').lower()}",
            f"TYPE: {self.task_type.value}",
            f"JOB: {self.job_name}",
            f"ATTEMPT: {self.attempt} of {self.max_attempts}",
            f"PRIORITY: {self.priority}",
            "",
            "**Error output:**",
            "```",
            self.error_output,
            "```",
            "",
            "**What to do:**",
            self.instructions,
            "",
        ]
        if self.context:
            lines.extend(["**Context:**", self.context, ""])
        lines.append(TASK_CLOSE)
        return "\n".join(lines)


@dataclass
class CopilotResult:
    status: ResultStatus
    changes: str = ""
    tests: str = ""
    commit: str = ""
    blocker: str = ""

    @classmethod
    def parse(cls, comment_body: str) -> CopilotResult | None:
        match = re.search(
            rf"{re.escape(RESULT_OPEN)}(.*?){re.escape(RESULT_CLOSE)}",
            comment_body,
            re.DOTALL,
        )
        if not match:
            return None

        block = match.group(1)
        status = ResultStatus.UNKNOWN
        changes = ""
        tests = ""
        commit = ""
        blocker = ""

        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("RESULT:"):
                raw = line.split(":", 1)[1].strip().upper()
                try:
                    status = ResultStatus(raw)
                except ValueError:
                    status = ResultStatus.UNKNOWN
            elif line.startswith("CHANGES:"):
                changes = line.split(":", 1)[1].strip()
            elif line.startswith("TESTS:"):
                tests = line.split(":", 1)[1].strip()
            elif line.startswith("COMMIT:"):
                commit = line.split(":", 1)[1].strip()
            elif line.startswith("BLOCKED:") or line.startswith("BLOCKER:"):
                blocker = line.split(":", 1)[1].strip()

        return cls(
            status=status,
            changes=changes,
            tests=tests,
            commit=commit,
            blocker=blocker,
        )


class CopilotProtocol:
    """Manages structured communication with Copilot via GitHub comments."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo

    async def post_task(self, pr_number: int, task: CopilotTask) -> Comment:
        body = task.to_comment()
        logger.info(
            "Posting task to PR #%d: %s (attempt %d/%d)",
            pr_number,
            task.task_type.value,
            task.attempt,
            task.max_attempts,
        )
        return await self._github.add_issue_comment(self._owner, self._repo, pr_number, body)

    async def find_latest_result(
        self, pr_number: int, after_comment_id: int | None = None
    ) -> CopilotResult | None:
        comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        for comment in reversed(comments):
            if after_comment_id and comment.id <= after_comment_id:
                break
            if comment.is_maintainer_result:
                result = CopilotResult.parse(comment.body)
                if result:
                    return result
        return None

    async def count_task_attempts(self, pr_number: int) -> int:
        comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        return sum(1 for c in comments if c.is_maintainer_task)
