"""Tests for the size classifier's pre/post-flight gates."""

from __future__ import annotations

from caretaker.foundry.size_classifier import Decision, post_flight, pre_flight


class TestPreFlight:
    def test_eligible_task_routes_foundry(self) -> None:
        result = pre_flight(
            task_type="LINT_FAILURE",
            allowed_task_types=["LINT_FAILURE", "REVIEW_COMMENT"],
            head_repo_full_name="org/repo",
            base_repo_full_name="org/repo",
            route_same_repo_only=True,
            error_output="short error",
        )
        assert result.decision == Decision.ROUTE_FOUNDRY

    def test_disallowed_task_type_escalates(self) -> None:
        result = pre_flight(
            task_type="TEST_FAILURE",
            allowed_task_types=["LINT_FAILURE"],
            head_repo_full_name="org/repo",
            base_repo_full_name="org/repo",
            route_same_repo_only=True,
            error_output="",
        )
        assert result.decision == Decision.ESCALATE_COPILOT
        assert "allowlist" in result.reason

    def test_fork_pr_escalates(self) -> None:
        result = pre_flight(
            task_type="LINT_FAILURE",
            allowed_task_types=["LINT_FAILURE"],
            head_repo_full_name="fork/repo",
            base_repo_full_name="org/repo",
            route_same_repo_only=True,
            error_output="",
        )
        assert result.decision == Decision.ESCALATE_COPILOT
        assert "fork" in result.reason

    def test_large_error_escalates(self) -> None:
        result = pre_flight(
            task_type="LINT_FAILURE",
            allowed_task_types=["LINT_FAILURE"],
            head_repo_full_name="org/repo",
            base_repo_full_name="org/repo",
            route_same_repo_only=True,
            error_output="x" * 20_000,
        )
        assert result.decision == Decision.ESCALATE_COPILOT


class TestPostFlight:
    def test_within_budget_allows_push(self) -> None:
        result = post_flight(
            files_changed=2,
            insertions=10,
            deletions=5,
            max_files_touched=10,
            max_diff_lines=400,
        )
        assert result.decision == Decision.ROUTE_FOUNDRY

    def test_too_many_files_escalates(self) -> None:
        result = post_flight(
            files_changed=20,
            insertions=1,
            deletions=1,
            max_files_touched=10,
            max_diff_lines=400,
        )
        assert result.decision == Decision.ESCALATE_COPILOT
        assert "files" in result.reason

    def test_too_many_lines_escalates(self) -> None:
        result = post_flight(
            files_changed=2,
            insertions=500,
            deletions=100,
            max_files_touched=10,
            max_diff_lines=400,
        )
        assert result.decision == Decision.ESCALATE_COPILOT
        assert "lines" in result.reason
