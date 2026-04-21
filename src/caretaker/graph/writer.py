"""Event-driven graph writer — Milestone M1 of the memory-graph plan.

The pre-existing :class:`~caretaker.graph.builder.GraphBuilder` rebuilds the
entire Neo4j graph every 60 seconds from authoritative caretaker state. That
is fine for the admin dashboard but it loses the temporal signal ("when did
the agent know this?") and makes it impossible for agents to publish facts
as they happen. This module adds a second, additive write path: a process
-wide :class:`GraphWriter` that exposes a small, non-blocking API for call
sites to record nodes and edges as events occur. The daily full-sync stays
as a reconciliation pass that heals any drift from dropped writes.

Design notes
------------

* The writer is a singleton. Callers fetch it via :func:`get_writer`. When
  Neo4j is not configured — for example in unit tests, or when the operator
  has opted out of the graph backend — the writer is in "disabled" mode and
  every ``record_*`` call is a cheap no-op.
* Enqueue is synchronous and never blocks. The background drain task, which
  owns the only ``GraphStore`` reference, consumes batches and writes them
  asynchronously. If the Neo4j cluster is unreachable the drain retries the
  batch a bounded number of times then drops it. The reconciliation pass is
  the backstop for dropped batches.
* Every ``record_*`` helper stamps an ``observed_at`` property on the
  payload. Bitemporal ``valid_from`` / ``valid_to`` are populated by M2 call
  sites that know the semantic validity window; the writer does not invent
  them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.graph.store import GraphStore

logger = logging.getLogger(__name__)


# ── Operation payloads ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _NodeOp:
    label: str
    node_id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _EdgeOp:
    source_label: str
    source_id: str
    target_label: str
    target_id: str
    rel_type: str
    properties: dict[str, Any] = field(default_factory=dict)


_Op = _NodeOp | _EdgeOp


# ── Writer ────────────────────────────────────────────────────────────────


class GraphWriter:
    """Async background graph writer.

    The writer owns a thread-safe queue that sync call sites push into, plus
    an asyncio task that drains the queue in batches. Callers never await
    the actual Neo4j write — that happens in the background. Tests can call
    :meth:`flush` to wait for the queue to drain.

    ``enabled=False`` (the default before :meth:`configure` is called) makes
    every ``record_*`` call a no-op. This keeps the module import-safe in
    environments that have no Neo4j — a common case in unit tests and in
    fleet consumers that only use the heartbeat emitter.
    """

    # Drop batches after this many consecutive failures. The daily full-sync
    # will pick up the missing rows.
    _MAX_RETRIES = 3
    # Drain cycle — how often the background task polls the queue. Cheap
    # because the queue poll itself is a ``Queue.get(timeout=...)``.
    _DRAIN_INTERVAL_SECONDS = 0.25
    # Upper bound on a single batch so one slow write doesn't stall the run.
    _BATCH_SIZE = 64

    def __init__(self) -> None:
        self._queue: queue.Queue[_Op] = queue.Queue()
        self._store: GraphStore | None = None
        self._enabled: bool = False
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = threading.Lock()
        # Batches taken off the queue are tracked here so that
        # :meth:`flush` knows work is still in flight and does not return
        # early just because the queue is empty.
        self._in_flight = 0
        # Counters surfaced to operators via ``stats()``.
        self._written_nodes = 0
        self._written_edges = 0
        self._dropped_ops = 0

    # ── Lifecycle ────────────────────────────────────────────────────────

    def configure(self, store: GraphStore) -> None:
        """Attach a live ``GraphStore`` and enable writes.

        Idempotent — calling it twice with the same store is a no-op.
        """
        with self._lock:
            if self._store is store and self._enabled:
                return
            self._store = store
            self._enabled = True

    async def start(self) -> None:
        """Start the background drain task. Requires :meth:`configure`."""
        if not self._enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._drain_loop(), name="graph-writer-drain")
        logger.info("GraphWriter drain task started")

    async def stop(self) -> None:
        """Stop the background drain task. Drains pending ops first."""
        if self._task is None:
            return
        # Give the loop a chance to flush what's already queued before we
        # signal stop — otherwise :meth:`flush` semantics become racy.
        await self.flush()
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        self._stop_event = None
        logger.info("GraphWriter drain task stopped")

    async def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty or ``timeout`` elapses.

        Returns ``True`` if the queue drained, ``False`` on timeout. Useful
        for tests and for the end-of-run shutdown path.
        """
        if not self._enabled:
            return True
        deadline = asyncio.get_event_loop().time() + timeout
        while self._queue.qsize() > 0 or self._in_flight > 0:
            if asyncio.get_event_loop().time() > deadline:
                return False
            await asyncio.sleep(self._DRAIN_INTERVAL_SECONDS / 2)
        return True

    def disable(self) -> None:
        """Turn the writer into a no-op. Used by tests between cases."""
        with self._lock:
            self._enabled = False
            self._store = None
            self._in_flight = 0
            # Drop any queued ops — tests should not leak across cases.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    # ── Public record API ────────────────────────────────────────────────

    def record_node(
        self,
        label: str,
        node_id: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a node merge. Non-blocking, safe when disabled."""
        if not self._enabled:
            return
        props = self._stamp(dict(properties or {}))
        self._queue.put(_NodeOp(label=label, node_id=node_id, properties=props))

    def record_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue an edge merge. Non-blocking, safe when disabled.

        ``properties`` should carry ``valid_from`` / ``valid_to`` when the
        call site knows the semantic validity window. The writer will
        always stamp ``observed_at`` so bitemporal queries work without
        requiring every caller to pass it.
        """
        if not self._enabled:
            return
        props = self._stamp(dict(properties or {}))
        self._queue.put(
            _EdgeOp(
                source_label=source_label,
                source_id=source_id,
                target_label=target_label,
                target_id=target_id,
                rel_type=rel_type,
                properties=props,
            )
        )

    def stats(self) -> dict[str, int]:
        """Return counters suitable for admin dashboards + tests."""
        return {
            "enabled": int(self._enabled),
            "queued": self._queue.qsize(),
            "written_nodes": self._written_nodes,
            "written_edges": self._written_edges,
            "dropped_ops": self._dropped_ops,
        }

    # ── Internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _stamp(props: dict[str, Any]) -> dict[str, Any]:
        """Stamp ``observed_at`` if the caller didn't supply one."""
        props.setdefault("observed_at", datetime.now(UTC).isoformat())
        return props

    async def _drain_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            batch = self._take_batch()
            if not batch:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._DRAIN_INTERVAL_SECONDS
                    )
                continue
            await self._write_batch(batch)

    def _take_batch(self) -> list[_Op]:
        batch: list[_Op] = []
        for _ in range(self._BATCH_SIZE):
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._in_flight += len(batch)
        return batch

    async def _write_batch(self, batch: list[_Op]) -> None:
        store = self._store
        if store is None:
            self._in_flight = max(0, self._in_flight - len(batch))
            return
        try:
            for op in batch:
                ok = await self._write_one(store, op)
                if ok and isinstance(op, _NodeOp):
                    self._written_nodes += 1
                elif ok and isinstance(op, _EdgeOp):
                    self._written_edges += 1
                elif not ok:
                    self._dropped_ops += 1
        finally:
            self._in_flight = max(0, self._in_flight - len(batch))

    async def _write_one(self, store: GraphStore, op: _Op) -> bool:
        for attempt in range(self._MAX_RETRIES):
            try:
                if isinstance(op, _NodeOp):
                    await store.merge_node(op.label, op.node_id, op.properties)
                else:
                    await store.merge_edge(
                        op.source_label,
                        op.source_id,
                        op.target_label,
                        op.target_id,
                        op.rel_type,
                        op.properties,
                    )
            except Exception as exc:  # neo4j driver raises a broad set
                if attempt + 1 >= self._MAX_RETRIES:
                    logger.warning(
                        "GraphWriter dropped op after %d retries: %s (%s)",
                        self._MAX_RETRIES,
                        type(exc).__name__,
                        exc,
                    )
                    return False
                await asyncio.sleep(0.2 * (attempt + 1))
            else:
                return True
        return False


# ── Module-level singleton ────────────────────────────────────────────────


_writer = GraphWriter()


def get_writer() -> GraphWriter:
    """Return the process-wide graph writer.

    Callers that produce graph facts should use this rather than threading
    a writer instance through every constructor. Until :meth:`configure` is
    called the writer is in no-op mode, so import-order does not matter.
    """
    return _writer


def reset_for_tests() -> None:
    """Test helper — disables the writer and drops queued ops."""
    _writer.disable()


__all__ = [
    "GraphWriter",
    "get_writer",
    "reset_for_tests",
]
