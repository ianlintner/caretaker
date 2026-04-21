"""Tests for M5 agent core-memory publish path.

Mirrors the ``RecordingStore`` fake pattern from
``tests/test_graph_builder_m3.py``: we wire the process-wide
:class:`~caretaker.graph.writer.GraphWriter` to an in-memory fake
store, drain the async queue, and assert the emitted node + edge
payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer, reset_for_tests
from caretaker.memory.core import AgentCoreMemory, publish


class RecordingStore:
    """Fake :class:`~caretaker.graph.store.GraphStore`."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []

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


def _sample_memory() -> AgentCoreMemory:
    return AgentCoreMemory(
        agent="pr_agent",
        run_id="2026-04-21T12:00:00+00:00",
        repo="acme/widgets",
        identity="pr_agent",
        active_goal="ci_health",
        active_pr=431,
        recent_action_ids=["edge-1", "edge-2", "edge-3"],
        context_tokens=450,
    )


@pytest.mark.asyncio
async def test_publish_emits_node_and_edge() -> None:
    """``publish`` lands exactly one AgentCoreMemory node + CORE_MEMORY_OF edge."""
    writer = get_writer()
    store = RecordingStore()
    writer.configure(store)  # type: ignore[arg-type]
    try:
        await writer.start()
        publish(_sample_memory())
        assert await writer.flush(timeout=2.0) is True

        acm_nodes = [n for n in store.nodes if n[0] == NodeType.AGENT_CORE_MEMORY.value]
        assert len(acm_nodes) == 1
        label, node_id, props = acm_nodes[0]
        assert label == "AgentCoreMemory"
        assert node_id == "acm:pr_agent:2026-04-21T12:00:00+00:00"
        assert props["agent"] == "pr_agent"
        assert props["repo"] == "acme/widgets"
        assert props["identity"] == "pr_agent"
        assert props["active_goal"] == "ci_health"
        assert props["active_pr"] == 431
        assert props["recent_action_ids"] == "edge-1,edge-2,edge-3"
        assert props["context_tokens"] == 450
        # observed_at is stamped by the writer.
        assert "observed_at" in props

        core_edges = [e for e in store.edges if e[4] == RelType.CORE_MEMORY_OF.value]
        assert len(core_edges) == 1
        src_label, src_id, tgt_label, tgt_id, rel, edge_props = core_edges[0]
        assert src_label == NodeType.AGENT_CORE_MEMORY.value
        assert tgt_label == NodeType.AGENT.value
        assert src_id == "acm:pr_agent:2026-04-21T12:00:00+00:00"
        assert tgt_id == "agent:pr_agent"
        assert rel == RelType.CORE_MEMORY_OF.value
        assert edge_props["repo"] == "acme/widgets"
        assert "observed_at" in edge_props
    finally:
        await writer.flush(timeout=2.0)
        await writer.stop()
        reset_for_tests()


@pytest.mark.asyncio
async def test_publish_is_noop_when_writer_disabled() -> None:
    """With the writer disabled, ``publish`` touches nothing."""
    writer = get_writer()
    store = RecordingStore()
    writer.configure(store)  # type: ignore[arg-type]
    writer.disable()  # explicitly disable
    try:
        publish(_sample_memory())
        # No drain running; the queue should still be empty since
        # disabled writers short-circuit enqueue.
        assert writer.stats()["queued"] == 0
        assert store.nodes == []
        assert store.edges == []
    finally:
        reset_for_tests()


@pytest.mark.asyncio
async def test_publish_replays_same_id_on_second_call() -> None:
    """Second publish for the same (agent, run_id) overwrites in place."""
    writer = get_writer()
    store = RecordingStore()
    writer.configure(store)  # type: ignore[arg-type]
    try:
        await writer.start()
        first = _sample_memory()
        publish(first)
        second = AgentCoreMemory(
            agent=first.agent,
            run_id=first.run_id,
            repo=first.repo,
            identity=first.identity,
            active_goal="build_health",
            active_pr=432,
            recent_action_ids=["edge-4"],
            context_tokens=200,
        )
        publish(second)
        assert await writer.flush(timeout=2.0) is True

        acm_nodes = [n for n in store.nodes if n[0] == NodeType.AGENT_CORE_MEMORY.value]
        # Two merge calls landed against the same node_id — the real
        # Neo4j store de-dupes via MERGE, but the writer passes both
        # through so the in-flight counter stays consistent.
        assert len(acm_nodes) == 2
        assert all(n[1] == "acm:pr_agent:2026-04-21T12:00:00+00:00" for n in acm_nodes)
        assert acm_nodes[-1][2]["active_goal"] == "build_health"
        assert acm_nodes[-1][2]["active_pr"] == 432
    finally:
        await writer.flush(timeout=2.0)
        await writer.stop()
        reset_for_tests()
