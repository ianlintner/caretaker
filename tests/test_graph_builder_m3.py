"""Tests for M3 of the memory-graph plan — tenant scoping + new node types.

M3 adds four node types (``:Repo``, ``:Comment``, ``:CheckRun``, ``:Executor``)
and threads an ``owner/name`` slug through the builder so every merged node
carries a ``repo`` scalar for tenant-scoped cypher queries. These tests
mirror the ``RecordingStore`` fake pattern used by the M2 suite.
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
    OwnershipState,
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


def _sample_state(*, owned_by: str = "caretaker") -> OrchestratorState:
    state = OrchestratorState()
    state.tracked_prs[42] = TrackedPR(
        number=42,
        state=PRTrackingState.CI_PASSING,
        ownership_state=OwnershipState.OWNED,
        owned_by=owned_by,
        ownership_acquired_at=datetime(2026, 4, 20, 11, 30, tzinfo=UTC),
    )
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


def _nodes_of(store: RecordingStore, label: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [n for n in store.nodes if n[0] == label]


@pytest.mark.asyncio
async def test_repo_node_merged_with_owner_name_slug() -> None:
    """A single ``:Repo`` node lands with the slug passed via ``repo=``."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    repos = _nodes_of(store, NodeType.REPO.value)
    assert len(repos) == 1
    _, node_id, props = repos[0]
    assert node_id == "repo:acme/widgets"
    assert props["slug"] == "acme/widgets"
    assert props["repo"] == "acme/widgets"


@pytest.mark.asyncio
async def test_repo_defaults_to_placeholder_when_omitted() -> None:
    """Legacy callers without the slug get the ``unknown/unknown`` fallback."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state())

    repos = _nodes_of(store, NodeType.REPO.value)
    assert len(repos) == 1
    assert repos[0][1] == "repo:unknown/unknown"
    assert repos[0][2]["slug"] == "unknown/unknown"


@pytest.mark.asyncio
async def test_every_emitted_node_has_repo_property() -> None:
    """Tenant scoping — every merged node advertises its ``repo`` slug."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    for label, node_id, props in store.nodes:
        assert "repo" in props, f"{label} node {node_id!r} missing repo scalar"
        assert props["repo"] == "acme/widgets"


@pytest.mark.asyncio
async def test_executor_node_and_handled_by_edge_for_copilot_pr() -> None:
    """``owned_by="copilot"`` mints an Executor node + PR-HANDLED_BY-Executor edge."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(owned_by="copilot"), repo="acme/widgets")

    executors = _nodes_of(store, NodeType.EXECUTOR.value)
    assert len(executors) == 1
    _, node_id, props = executors[0]
    assert node_id == "executor:copilot"
    assert props["provider"] == "copilot"
    assert props["repo"] == "acme/widgets"

    handled = _edges_of(store, RelType.HANDLED_BY.value)
    assert len(handled) == 1
    source_label, source_id, target_label, target_id, _, edge_props = handled[0]
    assert (source_label, target_label) == (NodeType.PR, NodeType.EXECUTOR)
    assert (source_id, target_id) == ("pr:42", "executor:copilot")
    # valid_from is stamped from ownership_acquired_at (M2 bitemporal).
    assert edge_props["valid_from"] == "2026-04-20T11:30:00+00:00"
    assert "observed_at" in edge_props


@pytest.mark.asyncio
async def test_no_executor_edge_for_caretaker_self_ownership() -> None:
    """``owned_by="caretaker"`` is the default (no delegation) — no Executor node."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(owned_by="caretaker"), repo="acme/widgets")

    assert _nodes_of(store, NodeType.EXECUTOR.value) == []
    assert _edges_of(store, RelType.HANDLED_BY.value) == []


@pytest.mark.asyncio
async def test_foundry_and_claude_code_also_mint_executors() -> None:
    """The full set of external providers is recognised."""
    for provider in ("foundry", "claude_code"):
        store = RecordingStore()
        await GraphBuilder(store).full_sync(_sample_state(owned_by=provider), repo="acme/widgets")
        executor_ids = {n[1] for n in _nodes_of(store, NodeType.EXECUTOR.value)}
        assert executor_ids == {f"executor:{provider}"}
        assert len(_edges_of(store, RelType.HANDLED_BY.value)) == 1


@pytest.mark.asyncio
async def test_repo_node_is_merged_before_other_nodes() -> None:
    """``:Repo`` is node 0 so downstream merges can reference it if needed."""
    store = RecordingStore()
    await GraphBuilder(store).full_sync(_sample_state(), repo="acme/widgets")

    assert store.nodes[0][0] == NodeType.REPO.value
    assert store.nodes[0][1] == "repo:acme/widgets"
