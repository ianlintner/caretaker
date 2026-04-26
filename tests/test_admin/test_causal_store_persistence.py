"""LRU bound, persistence, and Neo4j-fallback behaviour for :class:`CausalEventStore`.

These tests guard the bits added when we moved the causal store off
in-memory-only storage in v0.22.0:

* The cache is now bounded by ``CARETAKER_CAUSAL_CACHE_MAX_EVENTS``
  (default 5000) using ``OrderedDict`` LRU semantics.
* :meth:`CausalEventStore.refresh_from_github` no longer wipes the
  cache; events accumulate across ticks.
* When a graph backend is attached, writes flow through
  :class:`GraphWriter` and ``aget`` / ``awalk`` / ``adescendants`` fall
  back to Neo4j when the requested chain is not in the LRU cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

from caretaker.admin.causal_store import (
    CACHE_MAX_EVENTS_ENV,
    CausalEventStore,
    _causal_node_id,
    _event_node_props,
)
from caretaker.causal_chain import CausalEvent, CausalEventRef
from caretaker.graph.models import NodeType, RelType


class _FakeGraphStore:
    """Minimal async fake backing :class:`CausalEventStore`.

    Stores rows in a flat dict keyed by node id and accepts the
    ``where`` / ``params`` / ``order_by`` / ``limit`` arguments that the
    real :class:`GraphStore` supports. The ``CAUSED_BY`` traversal is
    simulated by following ``parent_id`` properties so the
    ``awalk`` / ``adescendants`` Neo4j fallbacks can be exercised
    without standing up a database.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def upsert(self, node_id: str, props: dict[str, Any]) -> None:
        merged = {**self.rows.get(node_id, {}), "id": node_id}
        merged.update({k: v for k, v in props.items() if v is not None})
        self.rows[node_id] = merged

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        assert label == "CausalEvent"
        params = params or {}
        target_id = params.get("id")
        rows = list(self.rows.values())
        if where and target_id is not None:
            if "CAUSED_BY*1" in where and "(n)-[" in where and "(:CausalEvent" in where:
                # Ancestors of target_id (walk parent chain).
                ancestors = self._ancestors_of(str(target_id))
                rows = [r for r in rows if r["id"] in ancestors]
            elif "CAUSED_BY*1" in where and "(:CausalEvent" in where and ")-[" in where:
                # Descendants of target_id (walk reverse parent chain).
                descendants = self._descendants_of(str(target_id))
                rows = [r for r in rows if r["id"] in descendants]
            elif where == "n.id = $id":
                rows = [r for r in rows if r["id"] == target_id]
        if order_by and "observed_at" in order_by:
            rows.sort(key=lambda r: r.get("observed_at") or "", reverse="DESC" in order_by)
        if limit is not None:
            rows = rows[: int(limit)]
        return rows

    def _ancestors_of(self, target_id: str) -> set[str]:
        result = {target_id}
        cursor = target_id
        for _ in range(64):
            row = self.rows.get(cursor)
            if not row:
                break
            parent = row.get("parent_id")
            if not parent:
                break
            parent_node = _causal_node_id(parent)
            if parent_node in result:
                break
            result.add(parent_node)
            cursor = parent_node
        return result

    def _descendants_of(self, target_id: str) -> set[str]:
        result = {target_id}
        frontier = [target_id]
        for _ in range(64):
            next_frontier: list[str] = []
            for node_id in frontier:
                event_id = node_id.removeprefix("causal:")
                for candidate in self.rows.values():
                    if candidate.get("parent_id") == event_id and candidate["id"] not in result:
                        result.add(candidate["id"])
                        next_frontier.append(candidate["id"])
            if not next_frontier:
                break
            frontier = next_frontier
        return result


class _RecordingWriter:
    """Captures GraphWriter calls so tests can inspect what got queued."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any] | None]] = []
        self.edges: list[tuple[str, str, str, str, str, dict[str, Any] | None]] = []

    def record_node(self, label: str, node_id: str, props: dict[str, Any] | None = None) -> None:
        self.nodes.append((label, node_id, props))

    def record_edge(
        self,
        src_label: str,
        src_id: str,
        tgt_label: str,
        tgt_id: str,
        rel_type: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        self.edges.append((src_label, src_id, tgt_label, tgt_id, rel_type, props))


@pytest.fixture
def recording_writer(monkeypatch: pytest.MonkeyPatch) -> _RecordingWriter:
    writer = _RecordingWriter()
    # Patch the lookup the store uses so writes flow into our recorder.
    monkeypatch.setattr("caretaker.admin.causal_store.get_writer", lambda: writer)
    return writer


def _make_event(
    event_id: str,
    *,
    parent: str | None = None,
    owner: str = "octo",
    repo: str = "demo",
) -> CausalEvent:
    return CausalEvent(
        id=event_id,
        source="test",
        parent_id=parent,
        ref=CausalEventRef(kind="issue", number=42, owner=owner, repo=repo),
        title="t",
        observed_at=None,
    )


class TestLRUCacheBound:
    def test_default_cap_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CACHE_MAX_EVENTS_ENV, "250")
        store = CausalEventStore()
        assert store.cache_cap == 250

    def test_evicts_oldest_when_over_cap(self, recording_writer: _RecordingWriter) -> None:
        # Tiny cap to make eviction trivial to assert on.
        store = CausalEventStore(cache_max_events=3)
        for idx in range(5):
            store.ingest(_make_event(f"evt-{idx}"))
        # Cache must hold the 3 most-recent ids; the first two are evicted.
        assert store.size() == 3
        ids = [e.id for e in store.index().values()]
        assert ids == ["evt-2", "evt-3", "evt-4"]
        assert store.stats()["evictions"] == 2

    def test_get_promotes_to_mru(self, recording_writer: _RecordingWriter) -> None:
        store = CausalEventStore(cache_max_events=3)
        for idx in range(3):
            store.ingest(_make_event(f"evt-{idx}"))
        # Touch evt-0 so it becomes most-recently-used; subsequent
        # insertion should evict evt-1 instead of evt-0.
        assert store.get("evt-0") is not None
        store.ingest(_make_event("evt-3"))
        ids = [e.id for e in store.index().values()]
        assert "evt-0" in ids
        assert "evt-1" not in ids


class TestPersistenceWiring:
    def test_ingest_writes_to_graph(self, recording_writer: _RecordingWriter) -> None:
        store = CausalEventStore(graph_store=_FakeGraphStore(), cache_max_events=10)
        store.ingest(_make_event("evt-1"))
        labels = [(label, node_id) for label, node_id, _ in recording_writer.nodes]
        assert (NodeType.CAUSAL_EVENT, "causal:evt-1") in labels
        # Repo node is auto-merged when owner+repo are set on the ref.
        assert (NodeType.REPO, "repo:octo/demo") in labels
        assert (RelType.BELONGS_TO, NodeType.CAUSAL_EVENT, NodeType.REPO) in {
            (rel, src, tgt) for src, _, tgt, _, rel, _ in recording_writer.edges
        }
        assert store.stats()["neo4j_writes"] == 1

    def test_parent_creates_caused_by_edge(self, recording_writer: _RecordingWriter) -> None:
        store = CausalEventStore(graph_store=_FakeGraphStore(), cache_max_events=10)
        store.ingest(_make_event("parent"))
        store.ingest(_make_event("child", parent="parent"))
        caused_by = [
            (sid, tid)
            for src, sid, tgt, tid, rel, _ in recording_writer.edges
            if rel == RelType.CAUSED_BY
        ]
        assert ("causal:child", "causal:parent") in caused_by

    @pytest.mark.asyncio
    async def test_warm_from_graph_respects_cap(self, recording_writer: _RecordingWriter) -> None:
        """warm_cache loads at most cache_cap rows ordered observed_at DESC."""
        graph = _FakeGraphStore()
        # Seed Neo4j with 5 events with monotonically increasing observed_at.
        for idx in range(5):
            event = CausalEvent(
                id=f"evt-{idx}",
                source="seed",
                parent_id=None,
                ref=CausalEventRef(kind="issue", number=1, owner="octo", repo="demo"),
                title="seeded",
                observed_at=None,
            )
            props = _event_node_props(event, "octo/demo")
            props["observed_at"] = f"2026-04-25T00:0{idx}:00+00:00"
            graph.upsert(_causal_node_id(event.id), props)

        store = CausalEventStore(graph_store=graph, cache_max_events=2)
        await store._warm_cache_from_graph(force=True)
        ids = [e.id for e in store.index().values()]
        # Newest two must be in cache; cache is also LRU-ordered with MRU at tail.
        assert set(ids) == {"evt-3", "evt-4"}
        # Older history is reachable via the async fetch path.
        oldest = await store.aget("evt-0")
        assert oldest is not None
        assert oldest.id == "evt-0"

    @pytest.mark.asyncio
    async def test_awalk_falls_back_to_neo4j(self, recording_writer: _RecordingWriter) -> None:
        """When the parent chain isn't in cache, awalk pulls it from Neo4j."""
        graph = _FakeGraphStore()
        # Build a 3-event chain in Neo4j only.
        chain: Iterable[tuple[str, str | None]] = (
            ("root", None),
            ("middle", "root"),
            ("leaf", "middle"),
        )
        for event_id, parent in chain:
            ev = CausalEvent(
                id=event_id,
                source="seed",
                parent_id=parent,
                ref=CausalEventRef(kind="issue", number=1, owner="octo", repo="demo"),
                title="t",
                observed_at=None,
            )
            graph.upsert(_causal_node_id(event_id), _event_node_props(ev, "octo/demo"))

        # Cache is empty: only the leaf id is asked for.
        store = CausalEventStore(graph_store=graph, cache_max_events=10)
        walk = await store.awalk("leaf")
        assert [e.id for e in walk.events] == ["root", "middle", "leaf"]

    @pytest.mark.asyncio
    async def test_adescendants_falls_back_to_neo4j(
        self, recording_writer: _RecordingWriter
    ) -> None:
        graph = _FakeGraphStore()
        for event_id, parent in (
            ("root", None),
            ("a", "root"),
            ("b", "root"),
            ("c", "a"),
        ):
            ev = CausalEvent(
                id=event_id,
                source="seed",
                parent_id=parent,
                ref=CausalEventRef(kind="issue", number=1, owner="octo", repo="demo"),
                title="t",
                observed_at=None,
            )
            graph.upsert(_causal_node_id(event_id), _event_node_props(ev, "octo/demo"))

        store = CausalEventStore(graph_store=graph, cache_max_events=10)
        kids = await store.adescendants("root")
        assert {e.id for e in kids} == {"a", "b", "c"}
