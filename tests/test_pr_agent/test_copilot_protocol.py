"""Tests for Copilot structured comment protocol."""

from __future__ import annotations

from project_maintainer.llm.copilot import (
    CopilotResult,
    CopilotTask,
    ResultStatus,
    TaskType,
    TASK_OPEN,
    TASK_CLOSE,
    RESULT_OPEN,
    RESULT_CLOSE,
)


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
