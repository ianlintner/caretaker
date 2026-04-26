"""Admin /health/doctor endpoint — bootstrap + wiring checks."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from caretaker.admin.auth import UserInfo, require_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "warning", "error"]


class DoctorCheck(BaseModel):
    name: str
    status: CheckStatus
    detail: str


class DoctorReport(BaseModel):
    checks: list[DoctorCheck]
    overall_status: CheckStatus


# Module-level deps injected at startup
_admin_data: Any = None
_graph_store: Any = None
_fleet_store: Any = None


def configure(
    admin_data: Any = None,
    graph_store: Any = None,
    fleet_store: Any = None,
) -> None:
    global _admin_data, _graph_store, _fleet_store  # noqa: PLW0603
    if admin_data is not None:
        _admin_data = admin_data
    if graph_store is not None:
        _graph_store = graph_store
    if fleet_store is not None:
        _fleet_store = fleet_store


@router.get("/health/doctor", response_model=DoctorReport)
async def doctor(
    _user: UserInfo = Depends(require_session),
) -> DoctorReport:
    """Run bootstrap / wiring checks and return their status."""
    checks: list[DoctorCheck] = []

    # 1. GITHUB_TOKEN / GITHUB_APP_ID
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_APP_ID")
    gh_detail = (
        "GITHUB_TOKEN or GITHUB_APP_ID present"
        if gh_token
        else "Neither GITHUB_TOKEN nor GITHUB_APP_ID is set"
    )
    checks.append(
        DoctorCheck(
            name="github_credentials",
            status="ok" if gh_token else "error",
            detail=gh_detail,
        )
    )

    # 2. OIDC config
    oidc_issuer = os.environ.get("OIDC_ISSUER") or os.environ.get("CARETAKER_OIDC_ISSUER")
    oidc_detail = (
        f"OIDC issuer: {oidc_issuer}"
        if oidc_issuer
        else "No OIDC issuer configured (auth may be open)"
    )
    checks.append(
        DoctorCheck(
            name="oidc_config",
            status="ok" if oidc_issuer else "warning",
            detail=oidc_detail,
        )
    )

    # 3. Admin data loaded
    admin_detail = (
        "AdminDataAccess is configured"
        if _admin_data is not None
        else "Admin data not configured — state polling may not be running"
    )
    checks.append(
        DoctorCheck(
            name="admin_data",
            status="ok" if _admin_data is not None else "error",
            detail=admin_detail,
        )
    )

    # 4. Graph store (Neo4j)
    graph_detail = (
        "GraphStore (Neo4j) connected"
        if _graph_store is not None
        else "GraphStore not configured — graph features disabled"
    )
    checks.append(
        DoctorCheck(
            name="graph_store",
            status="ok" if _graph_store is not None else "warning",
            detail=graph_detail,
        )
    )

    # 5. Fleet store
    fleet_detail = (
        "FleetRegistryStore configured"
        if _fleet_store is not None
        else "Fleet registry not configured"
    )
    checks.append(
        DoctorCheck(
            name="fleet_store",
            status="ok" if _fleet_store is not None else "warning",
            detail=fleet_detail,
        )
    )

    # 6. NEO4J_URI env
    neo4j_uri = os.environ.get("NEO4J_URI") or os.environ.get("CARETAKER_NEO4J_URI")
    neo4j_detail = (
        f"NEO4J_URI: {neo4j_uri}" if neo4j_uri else "NEO4J_URI not set — Neo4j features disabled"
    )
    checks.append(
        DoctorCheck(
            name="neo4j_uri",
            status="ok" if neo4j_uri else "warning",
            detail=neo4j_detail,
        )
    )

    # 7. CARETAKER_FLEET_SECRET
    fleet_secret = os.environ.get("CARETAKER_FLEET_SECRET")
    secret_detail = (
        "Fleet HMAC secret configured"
        if fleet_secret
        else "CARETAKER_FLEET_SECRET not set — fleet heartbeats unauthenticated"
    )
    checks.append(
        DoctorCheck(
            name="fleet_secret",
            status="ok" if fleet_secret else "warning",
            detail=secret_detail,
        )
    )

    # Determine overall status
    statuses = {c.status for c in checks}
    overall: CheckStatus = "ok"
    if "error" in statuses:
        overall = "error"
    elif "warning" in statuses:
        overall = "warning"

    return DoctorReport(checks=checks, overall_status=overall)


__all__ = ["configure", "router"]
