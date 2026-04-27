"""Factory — pick the right :class:`EventBus` from the environment.

Returns a process-wide singleton so callers reaching for the bus from
ad-hoc paths (e.g. the ``/runs/finish`` handler firing a self-heal
trigger) reuse the same Redis connection pool as the lifespan-spawned
consumer task. Without this each ``build_event_bus()`` call would
create a fresh ``RedisStreamsEventBus`` whose pool was never closed —
a slow connection leak on a busy backend.

Tests can swap or clear the singleton via :func:`set_event_bus` /
:func:`reset_event_bus`.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from caretaker.eventbus.local import LocalEventBus
from caretaker.eventbus.redis_streams import RedisStreamsEventBus

if TYPE_CHECKING:
    from caretaker.eventbus.base import EventBus

logger = logging.getLogger(__name__)


_singleton: EventBus | None = None


def build_event_bus(
    *,
    redis_url_env: str = "REDIS_URL",
    max_len_env: str = "CARETAKER_EVENT_BUS_MAXLEN",
    default_max_len: int = 100_000,
) -> EventBus:
    """Return the process-wide :class:`EventBus`, building on first call.

    The Redis backend is the production path; the local fallback exists
    so the backend boots cleanly in dev / unit tests without Redis.
    Subsequent calls return the same instance so the connection pool is
    reused — there is no per-call construction cost and no pool leak.
    """
    global _singleton  # noqa: PLW0603
    if _singleton is not None:
        return _singleton

    redis_url = os.environ.get(redis_url_env, "").strip()
    if not redis_url:
        logger.info("EventBus: Redis not configured — using in-process LocalEventBus")
        _singleton = LocalEventBus()
        return _singleton

    raw_max_len = os.environ.get(max_len_env, "").strip()
    try:
        max_len = int(raw_max_len) if raw_max_len else default_max_len
    except ValueError:
        logger.warning(
            "EventBus: invalid %s=%r — using default %d",
            max_len_env,
            raw_max_len,
            default_max_len,
        )
        max_len = default_max_len

    logger.info("EventBus: using Redis Streams (%s, max_len=%d)", redis_url_env, max_len)
    _singleton = RedisStreamsEventBus(redis_url=redis_url, max_len=max_len)
    return _singleton


def set_event_bus(bus: EventBus | None) -> None:
    """Test hook — replace the module-level singleton.

    Pass ``None`` to clear the singleton so the next ``build_event_bus()``
    call rebuilds from the environment.
    """
    global _singleton  # noqa: PLW0603
    _singleton = bus


def reset_event_bus() -> None:
    """Clear the singleton without setting a replacement."""
    set_event_bus(None)


__all__ = ["build_event_bus", "reset_event_bus", "set_event_bus"]
