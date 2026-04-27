"""In-process :class:`EventBus` for tests and Redis-less local dev.

Mirrors the at-least-once semantics of the Redis backend: a handler that
raises leaves the message in the pending list until the reaper claims it.
Single-process only — there is no cross-pod coordination, so this should
never be the production backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from caretaker.eventbus.base import Event, EventHandler

logger = logging.getLogger(__name__)


@dataclass
class _PendingEntry:
    event_id: str
    payload: dict[str, Any]
    delivered_at_ms: int
    consumer: str


@dataclass
class _StreamState:
    """Per-stream state — entries waiting to be read + per-group PELs."""

    queue: deque[tuple[str, dict[str, Any]]] = field(default_factory=deque)
    groups: dict[str, dict[str, _PendingEntry]] = field(default_factory=lambda: defaultdict(dict))
    new_data: asyncio.Event = field(default_factory=asyncio.Event)


class LocalEventBus:
    """In-process bus. Suitable for tests and dev — not multi-process safe."""

    def __init__(self) -> None:
        self._streams: dict[str, _StreamState] = defaultdict(_StreamState)
        self._counter = itertools.count(1)
        self._lock = asyncio.Lock()

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        async with self._lock:
            state = self._streams[stream]
            event_id = f"{int(time.time() * 1000)}-{next(self._counter)}"
            state.queue.append((event_id, dict(payload)))
            state.new_data.set()
            return event_id

    async def ensure_group(self, stream: str, group: str) -> None:
        async with self._lock:
            state = self._streams[stream]
            _ = state.groups[group]  # touch to materialise the defaultdict entry

    async def consume(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
        block_ms: int = 5_000,
        batch_size: int = 10,
    ) -> None:
        await self.ensure_group(stream, group)
        state = self._streams[stream]
        while True:
            # Wait for data with a bounded sleep so cancellation works.
            try:
                await asyncio.wait_for(state.new_data.wait(), timeout=block_ms / 1000)
            except TimeoutError:
                continue

            batch: list[tuple[str, dict[str, Any]]] = []
            async with self._lock:
                while state.queue and len(batch) < batch_size:
                    batch.append(state.queue.popleft())
                if not state.queue:
                    state.new_data.clear()
                pel = state.groups[group]
                now_ms = int(time.time() * 1000)
                for event_id, payload in batch:
                    pel[event_id] = _PendingEntry(
                        event_id=event_id,
                        payload=payload,
                        delivered_at_ms=now_ms,
                        consumer=consumer,
                    )

            for event_id, payload in batch:
                event = Event(id=event_id, stream=stream, payload=payload)
                try:
                    await handler(event)
                except Exception:
                    logger.warning(
                        "LocalEventBus handler raised stream=%s id=%s — left in PEL",
                        stream,
                        event_id,
                        exc_info=True,
                    )
                    continue
                async with self._lock:
                    state.groups[group].pop(event_id, None)

    async def claim_idle(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int = 300_000,
        handler: EventHandler,
    ) -> int:
        state = self._streams.get(stream)
        if state is None:
            return 0
        now_ms = int(time.time() * 1000)
        async with self._lock:
            pel = state.groups.get(group, {})
            stuck = [
                entry for entry in pel.values() if (now_ms - entry.delivered_at_ms) >= min_idle_ms
            ]

        handled = 0
        for entry in stuck:
            event = Event(id=entry.event_id, stream=stream, payload=entry.payload)
            try:
                await handler(event)
            except Exception:
                logger.warning(
                    "LocalEventBus claim_idle handler raised id=%s — left in PEL",
                    entry.event_id,
                    exc_info=True,
                )
                continue
            async with self._lock:
                state.groups.get(group, {}).pop(entry.event_id, None)
            handled += 1
        return handled

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            for state in self._streams.values():
                state.new_data.set()


__all__ = ["LocalEventBus"]
