"""Factory — pick the right :class:`EventBus` from the environment."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from caretaker.eventbus.local import LocalEventBus
from caretaker.eventbus.redis_streams import RedisStreamsEventBus

if TYPE_CHECKING:
    from caretaker.eventbus.base import EventBus

logger = logging.getLogger(__name__)


def build_event_bus(
    *,
    redis_url_env: str = "REDIS_URL",
    max_len_env: str = "CARETAKER_EVENT_BUS_MAXLEN",
    default_max_len: int = 100_000,
) -> EventBus:
    """Return a Redis-backed bus when ``REDIS_URL`` is set, else in-process.

    The Redis backend is the production path; the local fallback exists
    so the backend boots cleanly in dev / unit tests without Redis.
    """
    redis_url = os.environ.get(redis_url_env, "").strip()
    if not redis_url:
        logger.info("EventBus: Redis not configured — using in-process LocalEventBus")
        return LocalEventBus()

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
    return RedisStreamsEventBus(redis_url=redis_url, max_len=max_len)


__all__ = ["build_event_bus"]
