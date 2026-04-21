"""MCP memory adapter — §4.4 of ``docs/memory-graph-plan.md``.

Exposes three read-only HTTP endpoints that let external agents
(Claude Code, Cursor background agents, the ClaudeCodeExecutor) tap the
same memory surface caretaker uses internally:

* ``GET /api/mcp/memory/recent-actions`` — the per-agent subgraph of
  recent ``:Agent → AgentCoreMemory`` rows plus the agent's recent
  ``:Run EXECUTED`` edges.
* ``GET /api/mcp/memory/causal-chain/{event_id}`` — a thin wrapper over
  the existing :class:`~caretaker.admin.causal_store.CausalEventStore`
  walker + descendants BFS.
* ``GET /api/mcp/memory/skill-sop`` — returns the stored ``sop_text``
  for an exact ``(category, signature)`` skill hit.

All three require the same OIDC session cookie the admin dashboard
uses. The graph and skill endpoints surface a ``503`` when their
dependency (Neo4j / SQLite) has not been configured on this backend
replica — matching the pattern established by
``caretaker.admin.graph_api``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from caretaker.admin.auth import UserInfo, require_session
from caretaker.graph.models import NodeType, RelType

if TYPE_CHECKING:
    from caretaker.admin.causal_store import CausalEventStore
    from caretaker.evolution.insight_store import InsightStore
    from caretaker.graph.store import GraphStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/memory", tags=["mcp-memory"])


# ── Module-level singletons set during app startup ────────────────────────

_graph_store: GraphStore | None = None
_causal_store: CausalEventStore | None = None
_insight_store: InsightStore | None = None


def configure(
    graph_store: GraphStore | None = None,
    causal_store: CausalEventStore | None = None,
    insight_store: InsightStore | None = None,
) -> None:
    """Wire in the underlying stores.

    Called from :mod:`caretaker.mcp_backend.main` at startup. Any
    argument left as ``None`` disables the matching endpoints — they
    will return ``503`` until the dependency is configured.
    """
    global _graph_store, _causal_store, _insight_store  # noqa: PLW0603
    if graph_store is not None:
        _graph_store = graph_store
    if causal_store is not None:
        _causal_store = causal_store
    if insight_store is not None:
        _insight_store = insight_store


def reset_for_tests() -> None:
    """Test helper — drop all configured dependencies."""
    global _graph_store, _causal_store, _insight_store  # noqa: PLW0603
    _graph_store = None
    _causal_store = None
    _insight_store = None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/recent-actions")
async def recent_actions(
    agent: str = Query(..., description="Agent name (matches :Agent.id=agent:<name>)"),
    since: str | None = Query(
        default=None,
        description="ISO-8601 cutoff; only edges newer than this are returned",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the recent subgraph of an agent's core memory + run edges.

    Walks ``:Agent ← CORE_MEMORY_OF — :AgentCoreMemory`` plus the
    incoming ``:Run -[:EXECUTED]-> :Agent`` edges so callers see both
    "what did the agent carry forward?" (core memory) and "which runs
    invoked it?" (executed edges). Results are ordered newest-first by
    ``observed_at`` on the edge.
    """
    if _graph_store is None:
        raise HTTPException(status_code=503, detail="Graph store not configured")

    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=f"Invalid since timestamp: {err}") from err

    subgraph = await _graph_store.get_neighbors(f"agent:{agent}", depth=1)

    # Post-filter edges to the two relations and (optionally) the time
    # window. Keeping the filter here rather than in the cypher query
    # lets us reuse the existing ``get_neighbors`` helper — the admin
    # subgraph endpoint already ships the same shape to the frontend.
    keep_rels = {RelType.CORE_MEMORY_OF.value, RelType.EXECUTED.value}
    edges: list[dict[str, Any]] = []
    for edge in subgraph.edges:
        if edge.type not in keep_rels:
            continue
        if since_dt is not None:
            observed = edge.properties.get("observed_at")
            if isinstance(observed, str):
                try:
                    observed_dt = datetime.fromisoformat(observed)
                    if observed_dt.tzinfo is None:
                        observed_dt = observed_dt.replace(tzinfo=UTC)
                except ValueError:
                    continue
                if observed_dt <= since_dt:
                    continue
        edges.append(edge.model_dump())

    # Newest-first — best-effort; edges without observed_at sort to end.
    def _sort_key(edge: dict[str, Any]) -> str:
        props = edge.get("properties") or {}
        return str(props.get("observed_at", ""))

    edges.sort(key=_sort_key, reverse=True)
    return {"agent": agent, "edges": edges[:limit]}


@router.get("/causal-chain/{event_id}")
async def causal_chain(
    event_id: str,
    max_depth: int = Query(default=50, ge=1, le=500),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the root-first chain + descendants for a causal event."""
    if _causal_store is None:
        raise HTTPException(status_code=503, detail="Causal store not configured")

    if _causal_store.get(event_id) is None:
        raise HTTPException(status_code=404, detail=f"Causal event {event_id} not found")

    chain = _causal_store.walk(event_id, max_depth=max_depth)
    descendants = _causal_store.descendants(event_id, max_depth=max_depth)

    return {
        "id": event_id,
        "chain": [_event_to_dict(e) for e in chain.events],
        "truncated": chain.truncated,
        "descendants": [_event_to_dict(e) for e in descendants],
    }


@router.get("/skill-sop")
async def skill_sop(
    category: str = Query(..., description="Skill category (e.g. ``ci``)"),
    signature: str = Query(..., description="Exact skill signature"),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the SOP text for a specific skill (exact-signature hit)."""
    if _insight_store is None:
        raise HTTPException(status_code=503, detail="Insight store not configured")

    skill = _insight_store.get_by_signature(category, signature)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail=f"Skill {category}/{signature!r} not found",
        )
    return {
        "id": skill.id,
        "category": skill.category,
        "signature": skill.signature,
        "sop_text": skill.sop_text,
        "confidence": skill.confidence,
        "success_count": skill.success_count,
        "fail_count": skill.fail_count,
    }


# ── Helpers ───────────────────────────────────────────────────────────────


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Serialise a :class:`~caretaker.causal_chain.CausalEvent`."""
    ref = getattr(event, "ref", None)
    return {
        "id": event.id,
        "source": event.source,
        "parent_id": event.parent_id,
        "run_id": event.run_id,
        "title": getattr(event, "title", ""),
        "observed_at": event.observed_at.isoformat() if event.observed_at else None,
        "ref": (
            {
                "kind": ref.kind,
                "number": ref.number,
                "owner": ref.owner,
                "repo": ref.repo,
                "comment_id": getattr(ref, "comment_id", None),
            }
            if ref is not None
            else None
        ),
    }


__all__ = [
    "configure",
    "reset_for_tests",
    "router",
    # Re-export the enums so integration tests can assert on them without
    # pulling the graph-models module directly.
    "NodeType",
    "RelType",
]
