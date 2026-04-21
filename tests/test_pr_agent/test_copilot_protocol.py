"""Tests for Copilot structured comment protocol."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from caretaker.llm.copilot import (
    RESULT_CLOSE,
    RESULT_OPEN,
    TASK_CLOSE,
    TASK_OPEN,
    CopilotProtocol,
    CopilotResult,
    CopilotTask,
    ResultStatus,
    TaskType,
)
from caretaker.pr_agent.ci_triage import FailureType
from caretaker.pr_agent.copilot import _FAILURE_TYPE_TO_TASK_TYPE


class TestCopilotTask:
    def test_to_comment_format(self) -> None:
        task = CopilotTask(
            task_type=TaskType.TEST_FAILURE,
            job_name="test-unit",
            error_output="AssertionError in test_foo",
            instructions="Fix the test",
            attempt=1,
            max_attempts=2,
            priority="high",
        )
        body = task.to_comment()
        assert "@copilot" in body
        assert TASK_OPEN in body
        assert TASK_CLOSE in body
        assert "TASK: Fix test failure" in body
        assert "TYPE: TEST_FAILURE" in body
        assert "JOB: test-unit" in body
        assert "ATTEMPT: 1 of 2" in body
        assert "PRIORITY: high" in body
        assert "AssertionError in test_foo" in body
        assert "caretaker:causal" in body
        assert "source=pr-agent-task" in body

    def test_to_comment_with_context(self) -> None:
        task = CopilotTask(
            task_type=TaskType.LINT_FAILURE,
            job_name="lint",
            error_output="E501",
            instructions="Fix lint",
            attempt=1,
            max_attempts=2,
            context="This is context",
        )
        body = task.to_comment()
        assert "**Context:**" in body
        assert "This is context" in body

    def test_to_comment_without_context(self) -> None:
        task = CopilotTask(
            task_type=TaskType.GENERIC,
            job_name="ci",
            error_output="error",
            instructions="Fix it",
            attempt=1,
            max_attempts=2,
        )
        body = task.to_comment()
        assert "**Context:**" not in body


class TestCopilotResult:
    def test_parse_fixed(self) -> None:
        body = f"""
Some prelude text.

{RESULT_OPEN}
RESULT: FIXED
CHANGES: Updated test_foo.py
TESTS: All passing
COMMIT: abc123
{RESULT_CLOSE}
"""
        result = CopilotResult.parse(body)
        assert result is not None
        assert result.status == ResultStatus.FIXED
        assert result.changes == "Updated test_foo.py"
        assert result.tests == "All passing"
        assert result.commit == "abc123"

    def test_parse_blocked(self) -> None:
        body = f"""
{RESULT_OPEN}
RESULT: BLOCKED
BLOCKER: Cannot reproduce locally
{RESULT_CLOSE}
"""
        result = CopilotResult.parse(body)
        assert result is not None
        assert result.status == ResultStatus.BLOCKED
        assert result.blocker == "Cannot reproduce locally"

    def test_parse_no_result_block(self) -> None:
        body = "Just a regular comment"
        result = CopilotResult.parse(body)
        assert result is None

    def test_parse_unknown_status(self) -> None:
        body = f"""
{RESULT_OPEN}
RESULT: SOMETHING_WEIRD
{RESULT_CLOSE}
"""
        result = CopilotResult.parse(body)
        assert result is not None
        assert result.status == ResultStatus.UNKNOWN

    def test_parse_partial_status(self) -> None:
        body = f"""
{RESULT_OPEN}
RESULT: PARTIAL
CHANGES: Fixed 2 of 3 issues
{RESULT_CLOSE}
"""
        result = CopilotResult.parse(body)
        assert result is not None
        assert result.status == ResultStatus.PARTIAL
        assert result.changes == "Fixed 2 of 3 issues"


class TestFailureTypeToTaskTypeMapping:
    """Ensure every FailureType maps to a valid TaskType without raising ValueError."""

    @pytest.mark.parametrize(
        "failure_type,expected_task_type",
        [
            (FailureType.TEST_FAILURE, TaskType.TEST_FAILURE),
            (FailureType.LINT_FAILURE, TaskType.LINT_FAILURE),
            (FailureType.BUILD_FAILURE, TaskType.BUILD_FAILURE),
            (FailureType.TYPE_ERROR, TaskType.BUILD_FAILURE),
            (FailureType.TIMEOUT, TaskType.CI_FAILURE),
            (FailureType.BACKLOG, TaskType.CI_FAILURE),
            (FailureType.UNKNOWN, TaskType.CI_FAILURE),
        ],
    )
    def test_all_failure_types_map_to_valid_task_type(
        self, failure_type: FailureType, expected_task_type: TaskType
    ) -> None:
        result = _FAILURE_TYPE_TO_TASK_TYPE.get(failure_type, TaskType.CI_FAILURE)
        assert result == expected_task_type

    def test_all_failure_types_are_covered(self) -> None:
        """Every FailureType must have an entry in the mapping."""
        for ft in FailureType:
            assert ft in _FAILURE_TYPE_TO_TASK_TYPE, f"FailureType.{ft.name} missing from mapping"


@pytest.mark.asyncio
async def test_post_task_uses_copilot_token_for_pr_comment() -> None:
    github = AsyncMock()
    github.add_issue_comment.return_value = SimpleNamespace(id=99)
    protocol = CopilotProtocol(github, "o", "r")
    task = CopilotTask(
        task_type=TaskType.CI_FAILURE,
        job_name="ci",
        error_output="boom",
        instructions="fix it",
        attempt=1,
        max_attempts=2,
    )

    await protocol.post_task(42, task)

    github.add_issue_comment.assert_awaited_once()
    assert github.add_issue_comment.call_args.kwargs["use_copilot_token"] is True
