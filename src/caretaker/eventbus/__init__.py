"""Durable, multi-pod-safe event bus over Redis Streams + consumer groups.

See :mod:`caretaker.eventbus.base` for the Protocol and
:mod:`caretaker.eventbus.consumer` for the long-running webhook consumer.
"""

from caretaker.eventbus.base import Event, EventBus, EventBusError, EventHandler
from caretaker.eventbus.consumer import (
    DEFAULT_GROUP,
    DEFAULT_STREAM,
    run_trigger_event_payload,
    start_webhook_consumer,
    webhook_event_payload,
)
from caretaker.eventbus.factory import build_event_bus, reset_event_bus, set_event_bus
from caretaker.eventbus.local import LocalEventBus
from caretaker.eventbus.redis_streams import RedisStreamsEventBus

__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_STREAM",
    "Event",
    "EventBus",
    "EventBusError",
    "EventHandler",
    "LocalEventBus",
    "RedisStreamsEventBus",
    "build_event_bus",
    "reset_event_bus",
    "run_trigger_event_payload",
    "set_event_bus",
    "start_webhook_consumer",
    "webhook_event_payload",
]
