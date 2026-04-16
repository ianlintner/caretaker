"""Tests for orchestrator reconciliation behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.config import MaintainerConfig
from caretaker.orchestrator import Orchestrator
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)

if TYPE_CHECKING:
    import pathlib


def make_orchestrator() -> Orchestrator:
    github = AsyncMock()
    config = MaintainerConfig()
    return Orchestrator(config=config, github=github, owner="o", repo="r")


class TestOrchestratorReconciliation:
    def test_marks_issue_completed_when_linked_pr_merged(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                10: TrackedPR(number=10, state=PRTrackingState.MERGED),
            },
            tracked_issues={
                1: TrackedIssue(
                    number=1,
                    state=IssueTrackingState.PR_OPENED,
                    assigned_pr=10,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert state.tracked_issues[1].state == IssueTrackingState.COMPLETED

    def test_counts_orphaned_open_prs(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                20: TrackedPR(number=20, state=PRTrackingState.CI_PENDING),
                21: TrackedPR(number=21, state=PRTrackingState.MERGED),
            },
            tracked_issues={},
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.orphaned_prs == 1

    def test_does_not_count_escalated_pr_as_orphan(self) -> None:
        orchestrator = make_orchestrator()
        state = OrchestratorState(
            tracked_prs={
                22: TrackedPR(number=22, state=PRTrackingState.ESCALATED),
            },
            tracked_issues={},
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.orphaned_prs == 0

    def test_escalates_stale_assignments(self) -> None:
        orchestrator = make_orchestrator()
        old = datetime.now(UTC) - timedelta(days=14)
        state = OrchestratorState(
            tracked_issues={
                3: TrackedIssue(
                    number=3,
                    state=IssueTrackingState.ASSIGNED,
                    last_checked=old,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert state.tracked_issues[3].state == IssueTrackingState.ESCALATED
        assert state.tracked_issues[3].escalated is True
        assert summary.stale_assignments_escalated == 1

    def test_avg_merge_time_tolerates_mixed_naive_and_aware_datetimes(self) -> None:
        """merged_at from GitHub is timezone-aware; first_seen_at is naive UTC.
        _reconcile_state must not raise TypeError when computing avg_time_to_merge_hours."""
        orchestrator = make_orchestrator()
        naive_first_seen = datetime(2024, 1, 1, 0, 0, 0)
        aware_merged_at = datetime(2024, 1, 1, 2, 0, 0, tzinfo=UTC)
        state = OrchestratorState(
            tracked_prs={
                30: TrackedPR(
                    number=30,
                    state=PRTrackingState.MERGED,
                    first_seen_at=naive_first_seen,
                    merged_at=aware_merged_at,
                ),
            },
        )
        summary = RunSummary(mode="full")

        orchestrator._reconcile_state(state, summary)

        assert summary.avg_time_to_merge_hours == pytest.approx(2.0)


class TestOrchestratorReportPath:
    async def test_run_writes_json_report(self, tmp_path: pathlib.Path) -> None:
        """Orchestrator.run writes a JSON report when report_path is provided."""
        orchestrator = make_orchestrator()
        report_file = tmp_path / "report.json"

        # Stub out the registry and state so we can just test report writing
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run", report_path=str(report_file))

        assert report_file.exists()
        data = json.loads(report_file.read_text())
        assert "mode" in data
        assert "run_at" in data
        assert "errors" in data

    async def test_run_without_report_path_writes_no_file(self, tmp_path: pathlib.Path) -> None:
        """Orchestrator.run does not create any extra files when report_path is None."""
        orchestrator = make_orchestrator()

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run", report_path=None)

        # No unexpected JSON files in tmp_path
        assert list(tmp_path.glob("*.json")) == []

    async def test_charlie_mode_runs_only_charlie_agent(self) -> None:
        """Charlie mode invokes janitorial cleanup without running the broader cycle."""
        orchestrator = make_orchestrator()

        mock_agent = AsyncMock()
        mock_agent.name = "charlie"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(orchestrator._registry, "agents_for_mode", return_value=[mock_agent]),
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock) as mock_run_one,
        ):
            await orchestrator.run(mode="charlie")

        # Goal engine is disabled by default; legacy run_all path is taken
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "charlie"
        mock_run_one.assert_not_awaited()

    async def test_dry_run_dispatches_full_mode(self) -> None:
        """Dry-run should evaluate the same agent set as full mode."""
        orchestrator = make_orchestrator()

        mock_agent = AsyncMock()
        mock_agent.name = "pr"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(
                orchestrator._registry,
                "agents_for_mode",
                return_value=[mock_agent],
            ) as mock_afm,
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock),
        ):
            await orchestrator.run(mode="dry-run")

        # Goal engine is disabled by default; legacy run_all path is taken with 'full' mode
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "full"
        mock_afm.assert_not_called()

    async def test_self_heal_mode_forwards_event_payload(self) -> None:
        """Self-heal mode should pass event payload through to registry dispatch."""
        orchestrator = make_orchestrator()
        payload = {"workflow_run": {"id": 123}}

        mock_agent = AsyncMock()
        mock_agent.name = "self-heal"
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
            patch.object(orchestrator._registry, "agents_for_mode", return_value=[mock_agent]),
            patch.object(orchestrator._registry, "run_one", new_callable=AsyncMock) as mock_run_one,
        ):
            await orchestrator.run(mode="self-heal", event_payload=payload)

        # Goal engine is disabled by default; legacy run_all path is taken with payload forwarded
        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "self-heal"
        assert mock_run_all.call_args.kwargs.get("event_payload") == payload
        mock_run_one.assert_not_awaited()


class TestOrchestratorStateLoadFailure:
    async def test_run_returns_error_when_state_load_fails(self) -> None:
        """If state_tracker.load() raises, the run returns exit code 1 gracefully."""
        orchestrator = make_orchestrator()

        with (
            patch.object(
                orchestrator._state_tracker,
                "load",
                side_effect=Exception("GitHub API error 403: Rate limited."),
            ),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
        ):
            result = await orchestrator.run(mode="dry-run")

        assert result == 1

    async def test_run_does_not_raise_when_state_load_fails(self) -> None:
        """If state_tracker.load() raises, the run completes without propagating the exception."""
        orchestrator = make_orchestrator()

        with (
            patch.object(
                orchestrator._state_tracker,
                "load",
                side_effect=RuntimeError("connection refused"),
            ),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
        ):
            # Should not raise; errors are captured and reported via exit code
            result = await orchestrator.run(mode="dry-run")

        assert result == 1


class TestExtractPRNumber:
    """Tests for _extract_pr_number helper."""

    def test_pull_request_event_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"pull_request": {"number": 42}}
        assert _extract_pr_number("pull_request", payload) == 42

    def test_pull_request_review_event_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"pull_request": {"number": 55}, "review": {"state": "approved"}}
        assert _extract_pr_number("pull_request_review", payload) == 55

    def test_check_run_with_pr_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_run": {"pull_requests": [{"number": 77}]}}
        assert _extract_pr_number("check_run", payload) == 77

    def test_check_run_empty_prs_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_run": {"pull_requests": []}}
        assert _extract_pr_number("check_run", payload) is None

    def test_check_suite_with_pr_returns_number(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_suite": {"pull_requests": [{"number": 33}]}}
        assert _extract_pr_number("check_suite", payload) == 33

    def test_check_suite_empty_prs_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        payload = {"check_suite": {"pull_requests": []}}
        assert _extract_pr_number("check_suite", payload) is None

    def test_missing_key_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("pull_request", {}) is None

    def test_wrong_type_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("pull_request", {"pull_request": "bad"}) is None

    def test_unknown_event_returns_none(self) -> None:
        from caretaker.orchestrator import _extract_pr_number

        assert _extract_pr_number("workflow_run", {"workflow_run": {}}) is None
