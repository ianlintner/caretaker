"""Tests for orchestrator reconciliation behavior."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
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

        # Stub out all agent runners and state so we can just test report writing
        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator, "_run_pr_agent"),
            patch.object(orchestrator, "_run_issue_agent"),
            patch.object(orchestrator, "_run_upgrade_agent"),
            patch.object(orchestrator, "_run_devops_agent"),
            patch.object(orchestrator, "_run_security_agent"),
            patch.object(orchestrator, "_run_dependency_agent"),
            patch.object(orchestrator, "_run_docs_agent"),
            patch.object(orchestrator, "_run_stale_agent"),
            patch.object(orchestrator, "_run_escalation_agent"),
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
            patch.object(orchestrator, "_run_pr_agent"),
            patch.object(orchestrator, "_run_issue_agent"),
            patch.object(orchestrator, "_run_upgrade_agent"),
            patch.object(orchestrator, "_run_devops_agent"),
            patch.object(orchestrator, "_run_security_agent"),
            patch.object(orchestrator, "_run_dependency_agent"),
            patch.object(orchestrator, "_run_docs_agent"),
            patch.object(orchestrator, "_run_stale_agent"),
            patch.object(orchestrator, "_run_escalation_agent"),
        ):
            await orchestrator.run(mode="dry-run", report_path=None)

        # No unexpected JSON files in tmp_path
        assert list(tmp_path.glob("*.json")) == []


@pytest.mark.asyncio
class TestSelfHealMode:
    """self-heal mode invokes only the self-heal agent."""

    async def test_self_heal_mode_calls_self_heal_agent(self) -> None:
        orchestrator = make_orchestrator()

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator, "_run_self_heal_agent") as mock_self_heal,
            patch.object(orchestrator, "_run_pr_agent") as mock_pr,
            patch.object(orchestrator, "_run_issue_agent") as mock_issue,
        ):
            exit_code = await orchestrator.run(mode="self-heal")

        mock_self_heal.assert_awaited_once()
        mock_pr.assert_not_called()
        mock_issue.assert_not_called()
        assert exit_code == 0

    async def test_self_heal_mode_passes_event_payload(self) -> None:
        orchestrator = make_orchestrator()
        payload = {"workflow_run": {"id": 123, "conclusion": "failure", "head_branch": "main"}}

        with (
            patch.object(orchestrator._state_tracker, "load", return_value=OrchestratorState()),
            patch.object(orchestrator._state_tracker, "save"),
            patch.object(orchestrator._state_tracker, "post_run_summary"),
            patch.object(orchestrator, "_run_self_heal_agent") as mock_self_heal,
        ):
            await orchestrator.run(mode="self-heal", event_payload=payload)

        mock_self_heal.assert_awaited_once()
        _, kwargs = mock_self_heal.call_args
        assert kwargs.get("event_payload") == payload
