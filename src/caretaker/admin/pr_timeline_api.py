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

from fastapi import APIRouter, Depends
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


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.get("/pr/{owner}/{repo}/{number}/timeline", response_model=TimelineResponse)
async def get_pr_timeline(
    owner: str,
    repo: str,
    number: int,
    _user: UserInfo = Depends(require_session),
) -> TimelineResponse:
    """Return the chronological decision trail for one PR."""
    decisions: list[DecisionRow] = []
    if _store is not None:
        docs = await _store.query_timeline(owner=owner, repo=repo, pr_number=number)
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
    return TimelineResponse(
        owner=owner,
        repo=repo,
        pr_number=number,
        decisions=decisions,
    )


__all__ = ["DecisionRow", "TimelineResponse", "configure", "router"]
