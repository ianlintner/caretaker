"""Tests for CI failure triage."""

from __future__ import annotations

import pytest

from caretaker.github_client.models import CheckConclusion, CheckRun, CheckStatus
from caretaker.pr_agent.ci_triage import (
    FailureType,
    build_fix_instructions,
    classify_failure,
    triage_failure,
)

from tests.conftest import make_check_run


class TestClassifyFailure:
    def test_test_failure_by_name(self) -> None:
        cr = make_check_run(name="test-unit", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.TEST_FAILURE

    def test_pytest_in_name(self) -> None:
        cr = make_check_run(name="run-pytest", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.TEST_FAILURE

    def test_lint_failure(self) -> None:
        cr = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.LINT_FAILURE

    def test_ruff_in_name(self) -> None:
        cr = make_check_run(name="ruff-check", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.LINT_FAILURE

    def test_build_failure(self) -> None:
        cr = make_check_run(name="build", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.BUILD_FAILURE

    def test_mypy_type_error(self) -> None:
        cr = make_check_run(name="mypy", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.TYPE_ERROR

    def test_timeout(self) -> None:
        cr = make_check_run(name="integration", conclusion=CheckConclusion.TIMED_OUT)
        assert classify_failure(cr) == FailureType.TIMEOUT

    def test_unknown_job(self) -> None:
        cr = make_check_run(name="deploy-staging", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.UNKNOWN

    def test_output_title_classification(self) -> None:
        cr = make_check_run(
            name="ci",
            conclusion=CheckConclusion.FAILURE,
            output_title="pytest failed",
        )
        assert classify_failure(cr) == FailureType.TEST_FAILURE

    def test_eslint_in_name(self) -> None:
        cr = make_check_run(name="eslint-check", conclusion=CheckConclusion.FAILURE)
        assert classify_failure(cr) == FailureType.LINT_FAILURE


class TestBuildFixInstructions:
    def test_test_failure_instructions(self) -> None:
        cr = make_check_run(name="test-unit")
        instructions = build_fix_instructions(FailureType.TEST_FAILURE, cr)
        assert "test-unit" in instructions
        assert "RESULT block" in instructions

    def test_lint_failure_instructions(self) -> None:
        cr = make_check_run(name="lint")
        instructions = build_fix_instructions(FailureType.LINT_FAILURE, cr)
        assert "lint" in instructions

    def test_timeout_instructions(self) -> None:
        cr = make_check_run(name="integration")
        instructions = build_fix_instructions(FailureType.TIMEOUT, cr)
        assert "timed out" in instructions

    def test_unknown_instructions(self) -> None:
        cr = make_check_run(name="mystery-job")
        instructions = build_fix_instructions(FailureType.UNKNOWN, cr)
        assert "mystery-job" in instructions


class TestTriageFailure:
    @pytest.mark.asyncio
    async def test_triage_without_llm(self) -> None:
        cr = make_check_run(
            name="test-unit",
            conclusion=CheckConclusion.FAILURE,
            output_summary="FAILED test_foo.py::test_bar - AssertionError",
        )
        result = await triage_failure(cr, llm_router=None)
        assert result.failure_type == FailureType.TEST_FAILURE
        assert result.job_name == "test-unit"
        assert "AssertionError" in result.raw_output

    @pytest.mark.asyncio
    async def test_triage_uses_output(self) -> None:
        cr = make_check_run(
            name="lint",
            conclusion=CheckConclusion.FAILURE,
            output_summary="E501 line too long",
        )
        result = await triage_failure(cr)
        assert result.failure_type == FailureType.LINT_FAILURE
        assert result.error_summary == "E501 line too long"
