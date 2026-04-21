"""Tests for the event-driven graph writer (M1 of the memory-graph plan).

The writer is a process-wide singleton that call sites in
:mod:`caretaker.state`, :mod:`caretaker.evolution`, and future memory code
use to publish nodes and edges without blocking on the Neo4j driver.
These tests exercise it in isolation via a fake :class:`GraphStore`
stand-in so no Neo4j connection is required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from caretaker.graph.writer import GraphWriter, get_writer, reset_for_tests


class FakeGraphStore:
    """Minimal ``GraphStore`` replacement that records calls in memory."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any]]] = []
        self._remaining_failures = fail_times

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("simulated driver failure")
        self.nodes.append((label, node_id, properties))

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("simulated driver failure")
        self.edges.append(
            (source_label, source_id, target_label, target_id, rel_type, properties or {})
        )


@pytest.fixture(autouse=True)
def _reset_writer() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def test_record_before_configure_is_noop() -> None:
    """A disabled writer must never enqueue — safe for import-time calls."""
    writer = GraphWriter()
    writer.record_node("Run", "run:1", {"x": 1})
    writer.record_edge("Run", "run:1", "Goal", "goal:x", "AFFECTED")
    assert writer.stats()["queued"] == 0
    assert writer.stats()["enabled"] == 0


@pytest.mark.asyncio
async def test_configure_starts_drain_and_writes_nodes() -> None:
    store = FakeGraphStore()
    writer = GraphWriter()
    writer.configure(store)
    await writer.start()

    writer.record_node("Run", "run:1", {"mode": "full"})
    writer.record_node("Skill", "skill:abc", {"confidence": 0.5})

    assert await writer.flush(timeout=2.0)
    await writer.stop()

    labels = [n[0] for n in store.nodes]
    assert labels == ["Run", "Skill"]
    run_props = store.nodes[0][2]
    # observed_at is stamped automatically for bitemporal queries.
    assert "observed_at" in run_props
    assert run_props["mode"] == "full"


@pytest.mark.asyncio
async def test_records_edges_with_properties() -> None:
    store = FakeGraphStore()
    writer = GraphWriter()
    writer.configure(store)
    await writer.start()

    writer.record_edge(
        "Run",
        "run:1",
        "Goal",
        "goal:ci_health",
        "CONTRIBUTES_TO",
        {"score": 0.87, "valid_from": "2026-04-21T00:00:00+00:00"},
    )

    assert await writer.flush(timeout=2.0)
    await writer.stop()

    assert len(store.edges) == 1
    src_label, src_id, tgt_label, tgt_id, rel, props = store.edges[0]
    assert (src_label, src_id, tgt_label, tgt_id, rel) == (
        "Run",
        "run:1",
        "Goal",
        "goal:ci_health",
        "CONTRIBUTES_TO",
    )
    assert props["score"] == 0.87
    assert props["valid_from"] == "2026-04-21T00:00:00+00:00"
    assert "observed_at" in props


@pytest.mark.asyncio
async def test_retry_then_success_counted() -> None:
    """Transient driver failures are retried — real ones get dropped."""
    store = FakeGraphStore(fail_times=2)
    writer = GraphWriter()
    writer.configure(store)
    await writer.start()
    writer.record_node("Run", "run:1", {})
    assert await writer.flush(timeout=3.0)
    await writer.stop()
    assert len(store.nodes) == 1
    assert writer.stats()["written_nodes"] == 1
    assert writer.stats()["dropped_ops"] == 0


@pytest.mark.asyncio
async def test_exhausted_retries_increment_dropped_counter() -> None:
    """Permanent failures surface as a drop so operators can alert on them."""
    store = FakeGraphStore(fail_times=10)
    writer = GraphWriter()
    writer.configure(store)
    await writer.start()
    writer.record_node("Run", "run:lost", {})
    assert await writer.flush(timeout=5.0)
    await writer.stop()
    assert len(store.nodes) == 0
    assert writer.stats()["dropped_ops"] == 1


@pytest.mark.asyncio
async def test_disable_drops_queued_ops() -> None:
    """Tests must be able to reset the singleton between cases."""
    store = FakeGraphStore()
    writer = get_writer()
    writer.configure(store)
    writer.record_node("Run", "run:1", {})
    writer.disable()
    assert writer.stats()["queued"] == 0
    assert writer.stats()["enabled"] == 0


@pytest.mark.asyncio
async def test_singleton_is_shared_across_call_sites() -> None:
    """``get_writer()`` must return the same instance every call."""
    a = get_writer()
    b = get_writer()
    assert a is b


@pytest.mark.asyncio
async def test_flush_returns_false_on_timeout() -> None:
    """A slow store should be observable via ``flush`` returning ``False``."""

    class SlowStore(FakeGraphStore):
        async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
            await asyncio.sleep(1.0)
            await super().merge_node(label, node_id, properties)

    store = SlowStore()
    writer = GraphWriter()
    writer.configure(store)
    await writer.start()
    for i in range(20):
        writer.record_node("Run", f"run:{i}", {})
    flushed = await writer.flush(timeout=0.1)
    await writer.stop()
    # We expect ``False`` because the slow store can't drain 20 ops in 0.1s.
    assert flushed is False
