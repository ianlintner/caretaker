"""Tests for orchestrator reconciliation behavior."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

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
        old = datetime.utcnow() - timedelta(days=14)
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

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
        ):
            await orchestrator.run(mode="charlie")

        mock_run_all.assert_awaited_once()
        call_kwargs = mock_run_all.call_args
        assert call_kwargs[1].get("mode") == "charlie" or call_kwargs[0][2] == "charlie"

    async def test_dry_run_dispatches_full_mode(self) -> None:
        """Dry-run should evaluate the same agent set as full mode."""
        orchestrator = make_orchestrator()

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
        ):
            await orchestrator.run(mode="dry-run")

        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "full"

    async def test_self_heal_mode_forwards_event_payload(self) -> None:
        """Self-heal mode should pass event payload through to registry dispatch."""
        orchestrator = make_orchestrator()
        payload = {"workflow_run": {"id": 123}}

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator._registry, "run_all", new_callable=AsyncMock) as mock_run_all,
        ):
            await orchestrator.run(mode="self-heal", event_payload=payload)

        mock_run_all.assert_awaited_once()
        assert mock_run_all.call_args.kwargs.get("mode") == "self-heal"
        assert mock_run_all.call_args.kwargs.get("event_payload") == payload


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
