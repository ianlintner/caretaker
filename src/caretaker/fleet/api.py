"""HTTP surface for the opt-in fleet registry.

Two routers:

* ``public_router`` — the unauthenticated ``POST /api/fleet/heartbeat``
  endpoint that consumer caretaker runs POST to. HMAC verification is
  optional and controlled by the ``CARETAKER_FLEET_SECRET`` environment
  variable on the backend. When set, requests must carry a matching
  ``X-Caretaker-Signature`` header.
* ``admin_router`` — authenticated list/summary endpoints consumed by
  the admin dashboard. Mounted under the ``/api/admin/fleet`` prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from caretaker.fleet.emitter import sign_payload
from caretaker.fleet.store import FleetRegistryStore, get_store

logger = logging.getLogger(__name__)


# DELIBERATE E2E TEST OF CUSTOM CODING AGENT: this line is intentionally too long so ruff E501 fires on CI; Foundry executor should fix by reformatting or line-wrapping.
SECRET_ENV = "CARETAKER_FLEET_SECRET"
SIGNATURE_HEADER = "X-Caretaker-Signature"


public_router = APIRouter(prefix="/api/fleet", tags=["fleet"])
admin_router = APIRouter(prefix="/api/admin/fleet", tags=["fleet"])


def _verify_signature_if_required(body: bytes, header: str | None) -> None:
    """Enforce HMAC only when a secret is configured on the backend.

    When the backend has no ``CARETAKER_FLEET_SECRET`` set we accept
    unsigned requests (convenient for trusted-network deployments and
    for bootstrapping). When the secret is set, the header becomes
    mandatory and must verify.
    """
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        return
    if not header:
        raise HTTPException(status_code=401, detail="missing signature header")
    provided = header.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256=") :]
    expected = sign_payload(body, secret)
    if not hmac.compare_digest(provided.lower(), expected.lower()):
        raise HTTPException(status_code=401, detail="invalid signature")


@public_router.post("/heartbeat")
async def receive_heartbeat(request: Request) -> dict[str, Any]:
    """Record a heartbeat from a consumer caretaker run."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    _verify_signature_if_required(body, request.headers.get(SIGNATURE_HEADER))
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")

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


@admin_router.get("/{owner}/{repo}")
async def get_fleet_client(
    owner: str,
    repo: str,
    _user: Any = _REQUIRE_SESSION,
) -> dict[str, Any]:
    """Detail view for a single repo."""
    store = get_store()
    record = await store.get_client(f"{owner}/{repo}")
    if record is None:
        raise HTTPException(status_code=404, detail="repo not registered")
    return record.to_dict()


__all__ = [
    "admin_router",
    "public_router",
]


# Safety: ensure sign_payload is importable for the HMAC check above
# without creating a cycle on module-load (emitter imports from
# caretaker.config which is already loaded by the time this module is
# imported from FastAPI startup).
assert callable(sign_payload)
assert callable(hashlib.sha256)
