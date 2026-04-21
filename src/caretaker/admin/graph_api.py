"""Graph query API endpoints for the admin dashboard.

All endpoints require an authenticated OIDC session.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from caretaker.admin.auth import UserInfo, require_session
from caretaker.graph import compaction
from caretaker.graph.models import GraphStats, SubGraph  # noqa: TC001 (response models)
from caretaker.graph.store import GraphStore  # noqa: TC001 (runtime-used)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/graph", tags=["graph"])

_store: GraphStore | None = None


async def configure() -> None:
    """Initialise the graph store connection.  Called at app startup."""
    global _store  # noqa: PLW0603
    _store = GraphStore()
    await _store.ensure_indexes()


def _get_store() -> GraphStore:
    if _store is None:
        raise HTTPException(status_code=503, detail="Graph store not configured")
    return _store


@router.get("/stats")
async def graph_stats(
    _user: UserInfo = Depends(require_session),
) -> GraphStats:
    """Return node and edge counts by type."""
    return await _get_store().get_stats()


@router.get("/nodes")
async def list_nodes(
    node_type: str | None = Query(default=None, alias="type", description="Filter by node type"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _user: UserInfo = Depends(require_session),
) -> list[dict[str, Any]]:
    """Return nodes, optionally filtered by type."""
    nodes = await _get_store().get_nodes(node_type=node_type, limit=limit, offset=offset)
    return [n.model_dump() for n in nodes]


@router.get("/neighbors/{node_id}")
async def get_neighbors(
    node_id: str,
    depth: int = Query(default=1, ge=1, le=3),
    _user: UserInfo = Depends(require_session),
) -> SubGraph:
    """Return the neighborhood subgraph around a node."""
    return await _get_store().get_neighbors(node_id, depth=depth)


@router.get("/path/{from_id}/{to_id}")
async def shortest_path(
    from_id: str,
    to_id: str,
    _user: UserInfo = Depends(require_session),
) -> SubGraph:
    """Find the shortest path between two nodes."""
    return await _get_store().get_shortest_path(from_id, to_id)


@router.get("/subgraph")
async def get_subgraph(
    types: str | None = Query(default=None, description="Comma-separated node types to include"),
    limit: int = Query(default=200, ge=1, le=1000),
    _user: UserInfo = Depends(require_session),
) -> SubGraph:
    """Return a filtered subgraph for visualisation."""
    type_list = [t.strip() for t in types.split(",")] if types else None
    return await _get_store().get_subgraph(node_types=type_list, limit=limit)


@router.get("/agents/{agent_id}/impact")
async def agent_impact(
    agent_id: str,
    _user: UserInfo = Depends(require_session),
) -> SubGraph:
    """Return the full impact graph for an agent (PRs, issues, goals, skills)."""
    return await _get_store().get_neighbors(f"agent:{agent_id}", depth=2)


@router.get("/pr/{number}/lifecycle")
async def pr_lifecycle(
    number: int,
    _user: UserInfo = Depends(require_session),
) -> SubGraph:
    """Return the full lifecycle graph for a PR."""
    return await _get_store().get_neighbors(f"pr:{number}", depth=2)


# ── Compaction (M4) ────────────────────────────────────────────────────────


# Mounted on a sibling ``/api/admin`` path rather than ``/api/graph`` so
# it sits alongside the rest of the dashboard's state-mutation endpoints
# (see ``admin.api``). The OIDC session dependency is identical so the
# same allowlist governs who can kick off compaction.
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_router.post("/graph/compact")
async def trigger_graph_compaction(
    payload: dict[str, Any] = Body(..., description="{'repo': 'owner/name'}"),
    _user: UserInfo = Depends(require_session),
) -> dict[str, int]:
    """Run the nightly compaction pass on-demand.

    Rolls up the last ISO week of ``:Run`` nodes for ``repo`` into a
    single ``:RunSummaryWeek`` node then prunes low-salience tier-0
    rows older than 30 days. Returns the same counter dict that the
    24-hour heartbeat emits so operators can compare on-demand vs
    nightly runs.
    """
    repo = str(payload.get("repo", "")).strip()
    if not repo or "/" not in repo:
        raise HTTPException(
            status_code=400,
            detail="Body requires {'repo': '<owner>/<name>'}",
        )
    counts = await compaction.run_nightly(_get_store(), repo)
    logger.info("Manual graph compaction on %s: %s", repo, counts)
    return counts
