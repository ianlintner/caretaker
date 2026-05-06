"""Admin API for the per-PR decision timeline.

Exposes ``GET /api/admin/pr/{owner}/{repo}/{number}/timeline`` so the
admin SPA can render the full chronological list of decisions
caretaker made for a given PR. Reads from the
:class:`~caretaker.state.pr_decisions.PRDecisionStore` configured at
app startup.
"""

from __future__ import annotations

import logging
from datetime import datetime  # noqa: TC003 — pydantic resolves the annotation at runtime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from caretaker.admin.auth import UserInfo, require_session

if TYPE_CHECKING:
    from caretaker.state.pr_decisions import PRDecisionStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["pr-timeline"])

# Module-level store handle — set during app startup via ``configure``.
_store: PRDecisionStore | None = None


def configure(store: PRDecisionStore | None) -> None:
    """Install (or clear) the decision store backing the endpoint."""
    global _store  # noqa: PLW0603 — process singleton.
    _store = store


# ── Response schema ──────────────────────────────────────────────────────


class DecisionRow(BaseModel):
    id: str
    observed_at: datetime
    agent: str
    event: str
    fields: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None


class TimelineResponse(BaseModel):
    owner: str
    repo: str
    pr_number: int
    decisions: list[DecisionRow] = Field(default_factory=list)
    total_count: int = 0
    truncated: bool = False


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.get("/pr/{owner}/{repo}/{number}/timeline", response_model=TimelineResponse)
async def get_pr_timeline(
    owner: str,
    repo: str,
    number: int,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _user: UserInfo = Depends(require_session),
) -> TimelineResponse:
    """Return the chronological decision trail for one PR.

    ``limit``/``offset`` paginate the result. ``total_count`` is the
    full, unfiltered count of matching decisions so the SPA can render
    a "showing N of M" affordance; ``truncated`` is a convenience
    boolean (= ``total_count > offset + len(decisions)``) so callers
    don't need to recompute it client-side.
    """
    decisions: list[DecisionRow] = []
    total_count = 0
    if _store is not None:
        docs, total_count = await _store.query_timeline(
            owner=owner,
            repo=repo,
            pr_number=number,
            limit=limit,
            offset=offset,
        )
        for d in docs:
            decisions.append(
                DecisionRow(
                    id=str(d.get("id", "")),
                    observed_at=d["observed_at"],
                    agent=str(d.get("agent", "")),
                    event=str(d.get("event", "")),
                    fields=dict(d.get("fields") or {}),
                    trace_id=d.get("trace_id"),
                    span_id=d.get("span_id"),
                )
            )
    truncated = total_count > offset + len(decisions)
    return TimelineResponse(
        owner=owner,
        repo=repo,
        pr_number=number,
        decisions=decisions,
        total_count=total_count,
        truncated=truncated,
    )


__all__ = ["DecisionRow", "TimelineResponse", "configure", "router"]
