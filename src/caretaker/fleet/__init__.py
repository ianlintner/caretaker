"""Opt-in fleet-registry heartbeat emitter, store, and HTTP surface."""

from caretaker.fleet.api import admin_router, public_router
from caretaker.fleet.emitter import (
    FleetHeartbeat,
    build_heartbeat,
    emit_heartbeat,
    sign_payload,
)
from caretaker.fleet.store import (
    FleetClient,
    FleetRegistryStore,
    get_store,
    reset_store_for_tests,
)

__all__ = [
    "FleetClient",
    "FleetHeartbeat",
    "FleetRegistryStore",
    "admin_router",
    "build_heartbeat",
    "emit_heartbeat",
    "get_store",
    "public_router",
    "reset_store_for_tests",
    "sign_payload",
]
