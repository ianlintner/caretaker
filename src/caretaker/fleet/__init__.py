"""Opt-in fleet-registry heartbeat emitter, store, and HTTP surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from caretaker.fleet.abstraction import abstract_sop
from caretaker.fleet.alerts import (
    FleetAlert,
    FleetAlertStore,
    evaluate_fleet_alerts,
    get_alert_store,
    reset_alert_store_for_tests,
    upsert_fleet_alerts,
)
from caretaker.fleet.emitter import (
    AttributionSummary,
    FleetHeartbeat,
    FleetOAuthClientCache,
    build_heartbeat,
    emit_heartbeat,
    sign_payload,
)
from caretaker.fleet.graph import (
    GraphBackedGlobalSkillReader,
    promote_global_skills,
    sync_repos_to_graph,
)
from caretaker.fleet.store import (
    FleetClient,
    FleetRegistryStore,
    get_store,
    reset_store_for_tests,
)

# ``caretaker.fleet.api`` pulls in fastapi/starlette which are optional-only
# extras in pyproject. Expose its public names lazily so importing this package
# on minimal installs (no fastapi) still works for non-HTTP callers, e.g. the
# orchestrator's ``FleetOAuthClientCache`` usage.
if TYPE_CHECKING:
    from caretaker.fleet.api import (  # noqa: F401
        admin_router,
        public_router,
        set_fleet_alert_dependencies,
    )

_LAZY_API_NAMES = frozenset({"admin_router", "public_router", "set_fleet_alert_dependencies"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_API_NAMES:
        from caretaker.fleet import api as _api

        return getattr(_api, name)
    raise AttributeError(f"module 'caretaker.fleet' has no attribute {name!r}")


__all__ = [
    "AttributionSummary",
    "FleetAlert",
    "FleetAlertStore",
    "FleetClient",
    "FleetHeartbeat",
    "FleetOAuthClientCache",
    "FleetRegistryStore",
    "GraphBackedGlobalSkillReader",
    "abstract_sop",
    "admin_router",
    "build_heartbeat",
    "emit_heartbeat",
    "evaluate_fleet_alerts",
    "get_alert_store",
    "get_store",
    "promote_global_skills",
    "public_router",
    "reset_alert_store_for_tests",
    "reset_store_for_tests",
    "set_fleet_alert_dependencies",
    "sign_payload",
    "sync_repos_to_graph",
    "upsert_fleet_alerts",
]
