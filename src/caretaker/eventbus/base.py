"""EventBus abstraction — durable, multi-pod-safe message passing.

The MVP fan-out path was ``asyncio.create_task(dispatcher.dispatch(...))`` —
fire-and-forget in the FastAPI process. That works for a single replica
that never crashes mid-message, but as caretaker grew to multi-replica
deployments and longer-running agent work, two failure modes appeared:

* **Pod restart loses in-flight work.** The webhook was already 200-acked,
  so GitHub will not retry. The agent never runs.
* **No cross-replica load balancing.** Every replica that reaches the
  webhook does its own ``create_task``, which means deliveries never get
  shed when one replica is busy — they pile up locally until the
  in-flight cap drops them on the floor.

Both go away when fan-out goes through a durable queue with consumer
groups. The :class:`EventBus` Protocol describes the publish/consume
surface; concrete backends live in sibling modules
(:mod:`caretaker.eventbus.redis_streams`, :mod:`caretaker.eventbus.local`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Event:
    """A message read from the bus."""

    id: str
    """Backend-assigned message id (Redis Streams id, etc.). Treat as opaque."""

    stream: str
    payload: dict[str, Any]


EventHandler = Callable[[Event], Awaitable[None]]
"""Async handler invoked once per consumed event.

Successful return → bus acks. Raising → bus does NOT ack; the message
remains pending and is redelivered after the consumer-group claim-idle
threshold. Handlers should be idempotent: every consumer group offers
at-least-once, never exactly-once.
"""


class EventBusError(Exception):
    """Raised when an event-bus operation fails irrecoverably."""


class EventBus(Protocol):
    """Producer/consumer over a durable, replayable stream."""

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        """Append ``payload`` to ``stream``. Returns the assigned event id."""
        ...

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group if it does not exist (idempotent)."""
        ...

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
        """Read-and-handle loop. Runs until the surrounding task is cancelled."""
        ...

    async def claim_idle(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int = 300_000,
        handler: EventHandler,
    ) -> int:
        """Reclaim and re-handle messages stuck on dead consumers.

        Returns the count of messages handled this pass.
        """
        ...

    async def close(self) -> None:
        """Release any underlying connections."""
        ...


__all__ = [
    "Event",
    "EventBus",
    "EventBusError",
    "EventHandler",
]
