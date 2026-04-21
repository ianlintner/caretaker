"""Tests for M2 of the memory-graph plan — missing edges + bitemporal props.

The builder is still the reconciliation path (the M1 writer handles live
events), so the M2 edge catalog has to land in both places. These tests
exercise the builder's batch output against an in-memory fake store that
records every merge call — same pattern as the M1 writer tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.goals.models import GoalSnapshot
from caretaker.graph.builder import GraphBuilder
from caretaker.graph.models import NodeType, RelType
from caretaker.state.models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)


class RecordingStore:
    """Fake :class:`GraphStore` — records all merge calls."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []
        self.indexes_ensured = False

    async def ensure_indexes(self) -> None:
        self.indexes_ensured = True

    async def merge_node(self, label: str, node_id: str, props: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, props))

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.edges.append(
            (source_label, source_id, target_label, target_id, rel_type, properties or {})
        )


def _sample_state() -> OrchestratorState:
    state = OrchestratorState()
    state.tracked_prs[42] = TrackedPR(number=42, state=PRTrackingState.CI_PASSING)
    state.tracked_issues[7] = TrackedIssue(
        number=7,
        state=IssueTrackingState.PR_OPENED,
        assigned_pr=42,
    )
    state.goal_history["ci_health"] = [
        GoalSnapshot(goal_id="ci_health", score=0.9, status="satisfied"),
    ]
    state.run_history.append(
        RunSummary(
            run_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            mode="pr",
            prs_merged=1,
            goal_health=0.87,
            escalation_rate=0.1,
        )
    )
    return state


_EdgeTuple = tuple[str, str, str, str, str, dict[str, Any]]


def _edges_of(store: RecordingStore, rel: str) -> list[_EdgeTuple]:
    return [e for e in store.edges if e[4] == rel]


@pytest.mark.asyncio
async def test_pr_issue_references_and_resolved_by_edges() -> None:
    """Both directions of the PR↔Issue relationship land as separate edges."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state())

    references = _edges_of(store, RelType.REFERENCES.value)
    resolved = _edges_of(store, RelType.RESOLVED_BY.value)

    assert len(references) == 1
    assert (references[0][0], references[0][2]) == (NodeType.PR, NodeType.ISSUE)
    assert references[0][1] == "pr:42" and references[0][3] == "issue:7"

    assert len(resolved) == 1
    assert (resolved[0][0], resolved[0][2]) == (NodeType.ISSUE, NodeType.PR)
    assert resolved[0][1] == "issue:7" and resolved[0][3] == "pr:42"

    # Both edges carry bitemporal props stamped by the builder.
    for edge in (*references, *resolved):
        props = edge[5]
        assert "observed_at" in props
        assert "valid_from" in props


@pytest.mark.asyncio
async def test_run_executed_agent_edges_match_mode() -> None:
    """A ``mode="pr"`` run dispatches only the PR agent."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state())

    executed = _edges_of(store, RelType.EXECUTED.value)
    agent_targets = {e[3] for e in executed}
    assert agent_targets == {"agent:pr"}

    # run_at is the authoritative valid_from for run edges.
    props = executed[0][5]
    assert props["valid_from"] == "2026-04-20T12:00:00+00:00"


@pytest.mark.asyncio
async def test_run_affected_goal_carries_score() -> None:
    """Run→Goal AFFECTED edge ships both score and escalation context."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state())

    affected = _edges_of(store, RelType.AFFECTED.value)
    assert len(affected) == 1
    source_label, source_id, target_label, target_id, _, props = affected[0]
    assert source_label == NodeType.RUN
    assert target_label == NodeType.GOAL
    assert target_id == "goal:overall"
    assert props["score"] == pytest.approx(0.87)
    assert props["escalation_rate"] == pytest.approx(0.1)
    assert props["valid_from"] == "2026-04-20T12:00:00+00:00"


@pytest.mark.asyncio
async def test_overall_goal_node_is_synthesised() -> None:
    """Synthetic ``goal:overall`` exists so AFFECTED edges can MATCH it."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state())
    goal_ids = [n[1] for n in store.nodes if n[0] == NodeType.GOAL]
    assert "goal:overall" in goal_ids


@pytest.mark.asyncio
async def test_full_mode_run_executes_many_agents() -> None:
    """A ``mode="full"`` run must fan out to the full agent set."""
    state = _sample_state()
    state.run_history[-1] = RunSummary(
        run_at=datetime(2026, 4, 21, 0, 0, tzinfo=UTC),
        mode="full",
        goal_health=0.5,
    )
    store = RecordingStore()
    await GraphBuilder(store).full_sync(state)

    executed = _edges_of(store, RelType.EXECUTED.value)
    assert len(executed) >= 10  # at least 10 agents in the full fan-out
    assert {"agent:pr", "agent:issue", "agent:devops"}.issubset(e[3] for e in executed)
