"""HTTP surface for the opt-in fleet registry.

Two routers:

* ``public_router`` — the ``POST /api/fleet/heartbeat`` endpoint that
  consumer caretaker runs POST to. Authentication is enforced via OAuth2
  bearer tokens (JWTs) issued by the configured OIDC provider; tokens
  must carry the ``fleet:heartbeat`` scope. Configured at app startup via
  :func:`caretaker.auth.bearer.configure`.
* ``admin_router`` — authenticated list/summary endpoints consumed by
  the admin dashboard. Mounted under the ``/api/admin/fleet`` prefix.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from caretaker.auth.bearer import BearerPrincipal, require_bearer_token
from caretaker.fleet.alerts import (
    FleetAlertStore,
    evaluate_fleet_alerts,
    get_alert_store,
    upsert_fleet_alerts,
)
from caretaker.fleet.store import FleetRegistryStore, get_store

logger = logging.getLogger(__name__)


# Module-level shims for the admin fleet-alerts endpoint's optional
# dependencies. Populated by :func:`set_fleet_alert_dependencies` at admin
# startup; left as ``None`` when the fleet routers are mounted alone. Kept
# as globals (not FastAPI Depends) because the router is already a simple
# mountable surface that the other fleet endpoints don't parameterize.
_FLEET_ALERT_CONFIG: Any = None
_FLEET_ALERT_GRAPH: Any = None


public_router = APIRouter(prefix="/api/fleet", tags=["fleet"])
admin_router = APIRouter(prefix="/api/admin/fleet", tags=["fleet"])


# Bearer dependency for fleet heartbeats. Resolved at import time; verifies
# the JWT signature/issuer/expiry and requires the ``fleet:heartbeat`` scope.
# When :func:`caretaker.auth.bearer.configure` has not been called the
# dependency raises HTTP 503 — fail-closed.
_REQUIRE_FLEET_TOKEN = Depends(require_bearer_token("fleet:heartbeat"))


@public_router.post("/heartbeat")
async def receive_heartbeat(
    request: Request,
    principal: BearerPrincipal = _REQUIRE_FLEET_TOKEN,
) -> dict[str, Any]:
    """Record a heartbeat from a consumer caretaker run.

    Authentication: ``Authorization: Bearer <jwt>`` issued by the configured
    OIDC provider with scope ``fleet:heartbeat``.
    """
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

    # Stamp the verified caller onto the heartbeat for audit.  The store
    # ignores unknown fields, so this is safe to add unconditionally.
    payload.setdefault("authenticated_client_id", principal.client_id)

    store: FleetRegistryStore = get_store()
    try:
        record = await store.record_heartbeat(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, "repo": record.repo, "heartbeats_seen": record.heartbeats_seen}


# ── Admin (authenticated) endpoints ───────────────────────────────────────


def _auth_dependency() -> Any:
    """Import ``require_session`` lazily so fleet works even when the
    admin package isn't configured (e.g. headless MCP deployment)."""
    from caretaker.admin.auth import require_session

    return Depends(require_session)


# Module-level singleton. Resolved once at import time. Tests that want
# to bypass OIDC override the dependency with ``app.dependency_overrides``.
_REQUIRE_SESSION = _auth_dependency()


@admin_router.get("")
async def list_fleet(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    version: str | None = Query(default=None, description="Filter by caretaker version"),
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """Paginated list of known client repos, newest-heartbeat first."""
    store = get_store()
    clients = await store.list_clients()
    if version:
        clients = [c for c in clients if c.caretaker_version == version]
    total = len(clients)
    window = clients[offset : offset + limit]
    return {
        "items": [c.to_dict() for c in window],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/summary")
async def fleet_summary(
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """Small rollup for the dashboard header — total, version mix, stale."""
    from datetime import timedelta

    store = get_store()
    clients = await store.list_clients()
    stale = await store.stale_clients(threshold=timedelta(days=7))
    version_counts: dict[str, int] = {}
    for c in clients:
        version_counts[c.caretaker_version] = version_counts.get(c.caretaker_version, 0) + 1
    return {
        "total_clients": len(clients),
        "stale_clients": len(stale),
        "stale_threshold_days": 7,
        "version_distribution": version_counts,
    }


@admin_router.get("/alerts")
async def list_fleet_alerts(
    open_only: bool = Query(
        default=False,
        alias="open",
        description="Filter to open alerts only",
    ),
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """List fleet alerts.

    Runs the alert evaluator over every registered repo's recent heartbeat
    history, applies it to the admin-side alert store (which handles dedup
    + resolution), and returns the resulting rows.

    Set ``open=true`` to filter to alerts whose ``resolved_at`` is
    still ``None``.

    The evaluator is gated by :class:`~caretaker.config.FleetAlertConfig`
    via dependency injection of ``app.state.maintainer_config`` when the
    backend is configured with one; otherwise the evaluator runs with its
    pydantic defaults so the endpoint is useful even in a bootstrap /
    bare-registry deployment.
    """
    registry: FleetRegistryStore = get_store()
    alert_store: FleetAlertStore = get_alert_store()

    # Gather every repo's ring-buffer into a single list the evaluator can
    # group by repo itself.
    clients = await registry.list_clients()
    batch: list[Any] = []
    for client in clients:
        batch.extend(await registry.recent_heartbeats(client.repo))

    cfg_kwargs: dict[str, Any] = {}
    main_cfg = _FLEET_ALERT_CONFIG
    if main_cfg is not None:
        alerts_cfg = main_cfg.fleet.alerts
        if alerts_cfg.enabled:
            cfg_kwargs = {
                "goal_health_threshold": alerts_cfg.goal_health_threshold,
                "goal_health_n_consecutive": alerts_cfg.goal_health_n_consecutive,
                "error_spike_multiplier": alerts_cfg.error_spike_multiplier,
                "ghosted_window_days": alerts_cfg.ghosted_window_days,
            }
        else:
            # Feature disabled — return stored state without re-evaluating.
            stored = await alert_store.list(open_only=open_only)
            return {"items": [a.model_dump(mode="json") for a in stored]}

    evaluated = evaluate_fleet_alerts(batch, **cfg_kwargs)
    merged = await alert_store.apply(evaluated)

    # Best-effort graph mirror — ``upsert_fleet_alerts`` is fire-and-forget.
    upsert_fleet_alerts(merged, graph=_FLEET_ALERT_GRAPH)

    rows = await alert_store.list(open_only=open_only)
    return {"items": [a.model_dump(mode="json") for a in rows]}


def set_fleet_alert_dependencies(
    *,
    maintainer_config: Any | None = None,
    graph_store: Any | None = None,
) -> None:
    """Inject the admin-side config + graph store for the alert endpoint.

    Called once at admin app startup. Kept as module-level globals (rather
    than FastAPI ``Depends``) so the existing fleet routers retain their
    simple mountable shape — the admin dashboard already drives all other
    fleet endpoints the same way.
    """
    global _FLEET_ALERT_CONFIG, _FLEET_ALERT_GRAPH  # noqa: PLW0603
    _FLEET_ALERT_CONFIG = maintainer_config
    _FLEET_ALERT_GRAPH = graph_store


@admin_router.get("/{owner}/{repo}")
async def get_fleet_client(
    owner: str,
    repo: str,
    include_history: bool = False,
    history_limit: int = 32,
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """Detail view for a single repo.

    When ``include_history=true``, the response also includes the
    most recent heartbeats for this repo (oldest-first), capped at
    ``history_limit`` (default 32, the registry ring-buffer size).
    """
    store = get_store()
    slug = f"{owner}/{repo}"
    record = await store.get_client(slug)
    if record is None:
        raise HTTPException(status_code=404, detail="repo not registered")
    payload: dict[str, Any] = record.to_dict()
    if include_history:
        # Coerce heartbeats to JSON-safe primitives. ``recent_heartbeats``
        # may return datetime values for ``run_at`` depending on the
        # backing store; FastAPI/JSON cannot serialise those by default.
        history = await store.recent_heartbeats(slug, limit=max(1, history_limit))
        payload["history"] = [_jsonable_heartbeat(item) for item in history]
    return payload


def _jsonable_heartbeat(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serialisable copy of a heartbeat snapshot."""
    cleaned: dict[str, Any] = {}
    for key, value in snapshot.items():
        if hasattr(value, "isoformat"):
            cleaned[key] = value.isoformat()
        else:
            cleaned[key] = value
    return cleaned


__all__ = [
    "admin_router",
    "public_router",
]
