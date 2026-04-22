"""Opt-in fleet-registry heartbeat emitter, store, and HTTP surface."""

from caretaker.fleet.abstraction import abstract_sop
from caretaker.fleet.alerts import (
    FleetAlert,
    FleetAlertStore,
    evaluate_fleet_alerts,
    get_alert_store,
    reset_alert_store_for_tests,
    upsert_fleet_alerts,
)
from caretaker.fleet.api import admin_router, public_router, set_fleet_alert_dependencies
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
