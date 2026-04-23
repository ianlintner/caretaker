"""Admin API surface for shadow-mode decisions.

Exposes ``GET /api/admin/shadow/decisions`` so Phase 2 migrations can
start shipping disagreement data without blocking on the UI PR that
wires the frontend tab. Reads live Neo4j data when
:class:`~caretaker.config.GraphStoreConfig` is enabled; otherwise falls
back to the process-local ring buffer in
:mod:`caretaker.evolution.shadow` so the endpoint still returns
something in dev/test.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from caretaker.admin.auth import UserInfo, require_session
from caretaker.evolution.shadow import ShadowDecisionRecord, recent_records

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/shadow", tags=["shadow"])


# ── Module-level wiring (configured at startup) ──────────────────────────
#
# ``_graph_store`` is the live Neo4j ``GraphStore`` when the graph
# backend is enabled; ``None`` means we are running in dev fallback
# mode and the ring-buffer path should be used.

_graph_store: Any | None = None


def configure(graph_store: Any | None) -> None:
    """Install the graph store used for live Neo4j reads.

    Pass ``None`` to clear (tests / graph-disabled deployments). The
    parameter type is ``Any`` so unit tests can pass a minimal fake
    without importing :mod:`caretaker.graph.store`.
    """
    global _graph_store  # noqa: PLW0603 — process singleton.
    _graph_store = graph_store


# ── Response schema ──────────────────────────────────────────────────────


class ShadowDecisionRow(BaseModel):
    """One row of the ``/shadow/decisions`` response.

    Mirrors :class:`ShadowDecisionRecord` but normalises
    ``candidate_verdict_json`` so the empty-string Neo4j encoding maps
    back to ``None`` — clients should never have to special-case the
    backend storage layer.
    """

    id: str
    name: str
    repo_slug: str
    run_at: datetime
    outcome: str
    mode: str
    legacy_verdict_json: str
    candidate_verdict_json: str | None
    disagreement_reason: str | None
    context_json: str
    # Per-PR #503 follow-up: expose model attribution to the admin UI so
    # operators can tell at a glance whether a disagreement row was
    # produced by a model swap or a prompt change. Defaults to ``None``
    # so rows persisted before the field existed deserialise cleanly.
    legacy_model: str | None = None
    candidate_model: str | None = None

    @classmethod
    def from_record(cls, rec: ShadowDecisionRecord) -> ShadowDecisionRow:
        return cls(
            id=rec.id,
            name=rec.name,
            repo_slug=rec.repo_slug,
            run_at=rec.run_at,
            outcome=rec.outcome,
            mode=rec.mode,
            legacy_verdict_json=rec.legacy_verdict_json,
            candidate_verdict_json=rec.candidate_verdict_json,
            disagreement_reason=rec.disagreement_reason,
            context_json=rec.context_json,
            legacy_model=rec.legacy_model,
            candidate_model=rec.candidate_model,
        )


class ShadowDecisionsResponse(BaseModel):
    """Payload returned by ``GET /api/admin/shadow/decisions``."""

    items: list[ShadowDecisionRow] = Field(default_factory=list)
    agreement_rate: float = Field(
        default=1.0,
        description=(
            "Fraction of returned items where the two paths agreed. "
            "Defined as agree / (agree + disagree); ``candidate_error`` "
            "rows are excluded. When the denominator is zero (nothing to "
            "compare), the rate is 1.0 by convention."
        ),
    )
    agreement_rate_7d: float | None = Field(
        default=None,
        description=(
            "Rolling 7-day agreement rate from the nightly eval harness, "
            "populated when the request pins a single ``name``. ``None`` "
            "when there's no eval history yet."
        ),
    )
    agreement_rate_7d_by_site: dict[str, float | None] | None = Field(
        default=None,
        description=(
            "Per-site 7-day agreement rates when the request does not pin a "
            "single site. ``None`` when there is no eval history."
        ),
    )
    source: str = Field(
        description="Which backend produced the data: ``neo4j`` or ``ring_buffer``.",
    )


# ── Cypher read path ─────────────────────────────────────────────────────


_CYPHER_LIST = (
    "MATCH (s:ShadowDecision) "
    "WHERE ($name IS NULL OR s.name = $name) "
    "  AND ($since IS NULL OR s.run_at >= $since) "
    "RETURN s "
    "ORDER BY s.run_at DESC "
    "LIMIT $limit"
)


async def _read_from_neo4j(
    *, name: str | None, since: str | None, limit: int
) -> list[ShadowDecisionRow]:
    """Query Neo4j for the most recent ``:ShadowDecision`` nodes.

    ``since`` is passed through as an ISO-8601 string so Neo4j can do
    lexicographic comparisons against the ``run_at`` property without
    requiring a datetime type coercion on both ends.
    """
    assert _graph_store is not None  # caller guarantees
    # Use the same ``session`` pattern as :class:`GraphStore`.
    async with _graph_store._driver.session(database=_graph_store._database) as session:
        result = await session.run(_CYPHER_LIST, name=name, since=since, limit=limit)
        rows: list[ShadowDecisionRow] = []
        async for record in result:
            node = record["s"]
            props = dict(node)
            try:
                run_at = datetime.fromisoformat(props["run_at"])
            except (KeyError, ValueError):
                # Skip malformed nodes rather than 500 the whole request.
                continue
            candidate = props.get("candidate_verdict_json") or None
            reason = props.get("disagreement_reason") or None
            # Models are stored as empty-string-for-None in Neo4j (see the
            # write path in :func:`caretaker.evolution.shadow.write_shadow_decision`);
            # normalise back to ``None`` so the admin UI / tests don't
            # have to know about the storage-layer quirk. Missing keys
            # (pre-field rows) also land at ``None`` here.
            legacy_model = props.get("legacy_model") or None
            candidate_model = props.get("candidate_model") or None
            rows.append(
                ShadowDecisionRow(
                    id=str(props.get("id", "")),
                    name=str(props.get("name", "")),
                    repo_slug=str(props.get("repo_slug", "")),
                    run_at=run_at,
                    outcome=str(props.get("outcome", "")),
                    mode=str(props.get("mode", "")),
                    legacy_verdict_json=str(props.get("legacy_verdict_json", "")),
                    candidate_verdict_json=candidate,
                    disagreement_reason=reason,
                    context_json=str(props.get("context_json", "{}")),
                    legacy_model=legacy_model,
                    candidate_model=candidate_model,
                )
            )
        return rows


# ── Public endpoint ──────────────────────────────────────────────────────


def _compute_agreement_rate(rows: list[ShadowDecisionRow]) -> float:
    agree = sum(1 for r in rows if r.outcome == "agree")
    disagree = sum(1 for r in rows if r.outcome == "disagree")
    denom = agree + disagree
    if denom == 0:
        return 1.0
    return agree / denom


@router.get("/decisions", response_model=ShadowDecisionsResponse)
async def list_shadow_decisions(
    name: str | None = Query(
        default=None,
        description="Optional decision-site filter (e.g. ``readiness``).",
    ),
    since: datetime | None = Query(
        default=None,
        description="ISO-8601 lower bound on ``run_at`` (tz-aware).",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    _user: UserInfo = Depends(require_session),
) -> ShadowDecisionsResponse:
    """Return the most-recent shadow-mode decisions.

    Reads Neo4j when the graph backend is wired, otherwise falls back
    to the process-local ring buffer so the UI has something to render
    in dev.
    """
    # Normalise tz — Neo4j comparisons are string-level, so we want
    # ISO-8601 UTC on the way in.
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    if _graph_store is not None:
        since_str = since.isoformat() if since is not None else None
        try:
            rows = await _read_from_neo4j(name=name, since=since_str, limit=limit)
            return _build_response(rows=rows, name=name, source="neo4j")
        except Exception as exc:  # noqa: BLE001 — never 500 the admin UI
            logger.warning(
                "Neo4j read failed for shadow decisions, falling back to ring buffer: %s",
                exc,
            )

    records = recent_records(name=name, since=since, limit=limit)
    rows = [ShadowDecisionRow.from_record(r) for r in records]
    return _build_response(rows=rows, name=name, source="ring_buffer")


def _build_response(
    *, rows: list[ShadowDecisionRow], name: str | None, source: str
) -> ShadowDecisionsResponse:
    """Attach rolling 7-day agreement rates from :mod:`caretaker.eval.store`.

    The import is deferred so the shadow admin router still works in a
    deployment where the eval extra isn't installed (the store module
    itself has no optional deps, but keeping the coupling lazy means a
    future refactor can split the eval package out cleanly).
    """
    from caretaker.eval import store as eval_store

    resp = ShadowDecisionsResponse(
        items=rows,
        agreement_rate=_compute_agreement_rate(rows),
        source=source,
    )
    if name is not None:
        resp.agreement_rate_7d = eval_store.rolling_agreement_rate(name)
    else:
        latest = eval_store.latest_report()
        if latest is not None:
            resp.agreement_rate_7d_by_site = {
                s.site: eval_store.rolling_agreement_rate(s.site) for s in latest.sites
            }
    return resp


__all__ = [
    "ShadowDecisionRow",
    "ShadowDecisionsResponse",
    "configure",
    "router",
]
