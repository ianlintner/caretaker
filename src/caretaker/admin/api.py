"""Read-only admin REST API endpoints.

All endpoints require an authenticated OIDC session (enforced via the
``require_session`` dependency).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from caretaker.admin.auth import UserInfo, require_session
from caretaker.admin.data import (  # noqa: TC001 (runtime-resolved response models)
    AdminDataAccess,
    PaginatedResponse,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Module-level data access — set during app startup via ``configure()``.
_data: AdminDataAccess | None = None


def configure(data: AdminDataAccess) -> None:
    """Set the data access instance.  Called at app startup."""
    global _data  # noqa: PLW0603
    _data = data


def _get_data() -> AdminDataAccess:
    if _data is None:
        raise HTTPException(status_code=503, detail="Admin dashboard not initialised")
    return _data


# ── Orchestrator State ────────────────────────────────────────────────────


@router.get("/state")
async def get_state(
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the full OrchestratorState snapshot."""
    return _get_data().get_state()


@router.get("/prs")
async def list_prs(
    state: str | None = Query(default=None, description="Filter by PRTrackingState"),
    ownership: str | None = Query(default=None, description="Filter by OwnershipState"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List tracked pull requests with optional filters."""
    return _get_data().get_tracked_prs(
        state_filter=state, ownership_filter=ownership, offset=offset, limit=limit
    )


@router.get("/prs/{number}")
async def get_pr(
    number: int,
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Get a single tracked PR by number."""
    result = _get_data().get_tracked_pr(number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"PR #{number} not tracked")
    return result


@router.get("/issues")
async def list_issues(
    state: str | None = Query(default=None, description="Filter by IssueTrackingState"),
    classification: str | None = Query(default=None, description="Filter by classification"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List tracked issues with optional filters."""
    return _get_data().get_tracked_issues(
        state_filter=state, classification_filter=classification, offset=offset, limit=limit
    )


@router.get("/issues/{number}")
async def get_issue(
    number: int,
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Get a single tracked issue by number."""
    result = _get_data().get_tracked_issue(number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Issue #{number} not tracked")
    return result


# ── Run History ───────────────────────────────────────────────────────────


@router.get("/runs")
async def list_runs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List orchestrator run history."""
    return _get_data().get_run_history(offset=offset, limit=limit)


@router.get("/runs/latest")
async def latest_run(
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the most recent run summary."""
    result = _get_data().get_latest_run()
    if result is None:
        raise HTTPException(status_code=404, detail="No runs recorded yet")
    return result


@router.get("/metrics/fanout")
async def fanout_metrics(
    high_cycle_threshold: int = Query(default=2, ge=1, le=100),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Per-PR fan-out proxies (fix_cycles, copilot_attempts) for F1 monitoring."""
    return _get_data().get_fanout_metrics(high_cycle_threshold=high_cycle_threshold)


@router.get("/metrics/storm")
async def storm_metrics(
    window: int = Query(default=20, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Rolling self-heal + escalation counts across the most recent runs.

    Surfaces the F2 storm-detection metric: would have flagged the
    2026-04-14 108-PR-in-90-min incident near run #5.
    """
    return _get_data().get_storm_metrics(window_runs=window)


# ── Goals ─────────────────────────────────────────────────────────────────


@router.get("/goals")
async def get_goals(
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return goal score history for all goals."""
    return _get_data().get_goal_history()


# ── Memory Store ──────────────────────────────────────────────────────────


@router.get("/memory")
async def list_memory_namespaces(
    _user: UserInfo = Depends(require_session),
) -> list[dict[str, Any]]:
    """List all memory namespaces with entry counts."""
    return [ns.model_dump() for ns in _get_data().get_memory_namespaces()]


@router.get("/memory/{namespace}")
async def get_memory_entries(
    namespace: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List entries in a memory namespace."""
    return _get_data().get_memory_entries(namespace, offset=offset, limit=limit)


# ── Skills & Evolution ────────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(
    category: str | None = Query(default=None, description="Filter by category"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List learned skills."""
    return _get_data().get_skills(category=category, offset=offset, limit=limit)


@router.get("/skills/{skill_id}")
async def get_skill(
    skill_id: str,
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Get a single skill by ID."""
    result = _get_data().get_skill(skill_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Skill {skill_id} not found")
    return result


@router.get("/mutations")
async def list_mutations(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """List mutation trials."""
    return _get_data().get_mutations(offset=offset, limit=limit)


# ── Agents ────────────────────────────────────────────────────────────────


@router.get("/agents")
async def list_agents(
    _user: UserInfo = Depends(require_session),
) -> list[dict[str, Any]]:
    """List all registered agents with their modes and event triggers."""
    return [a.model_dump() for a in _get_data().get_agents()]


# ── Config ────────────────────────────────────────────────────────────────


@router.get("/config")
async def get_config(
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return the current configuration (secrets redacted)."""
    return _get_data().get_config()


# ── Causal chains ─────────────────────────────────────────────────────────


@router.get("/causal")
async def list_causal_events(
    source: str | None = Query(default=None, description="Filter by event source"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: UserInfo = Depends(require_session),
) -> PaginatedResponse:
    """Page through observed causal events, most recent first."""
    return _get_data().get_causal_events(source=source, offset=offset, limit=limit)


@router.get("/causal/{event_id}")
async def get_causal_chain(
    event_id: str,
    max_depth: int = Query(default=50, ge=1, le=500),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Walk the parent chain of ``event_id`` root-first."""
    result = _get_data().get_causal_chain(event_id, max_depth=max_depth)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Causal event {event_id} not found")
    return result


@router.get("/causal/{event_id}/descendants")
async def get_causal_descendants(
    event_id: str,
    max_depth: int = Query(default=50, ge=1, le=500),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return BFS descendants of ``event_id``."""
    result = _get_data().get_causal_descendants(event_id, max_depth=max_depth)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Causal event {event_id} not found")
    return result
