"""Tests for orchestrator reconciliation behavior."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

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
