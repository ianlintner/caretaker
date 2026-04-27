"""Admin API surface for eval-harness reports.

Exposes ``GET /api/admin/eval/latest?site=<name>`` so the admin UI can
render the most recent Braintrust experiment summary without proxying
through Braintrust itself — the experiment URL is in the response for
operators who want to drill in, but the local copy is authoritative
for the "is this site ready for ``enforce`` yet?" signal.

All state comes from :mod:`caretaker.eval.store`, the same process-local
store the enforce-gate CLI reads from.
"""

from __future__ import annotations

import logging
from datetime import datetime  # noqa: TC003 — used at runtime by pydantic response model
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from caretaker.admin.auth import UserInfo, require_session
from caretaker.eval import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/eval", tags=["eval"])


class ScorerSummaryRow(BaseModel):
    """One (scorer, mean, count) row inside a site report."""

    scorer: str
    mean: float
    count: int
    judge_disagreements: int = 0


class LatestSiteReportResponse(BaseModel):
    """Payload returned by ``GET /api/admin/eval/latest?site=...``."""

    site: str = Field(description="Decision-site name (e.g. ``readiness``).")
    generated_at: datetime = Field(
        description="When the nightly report was produced (tz-aware UTC)."
    )
    since: datetime
    until: datetime
    record_count: int
    agreement_rate: float
    agreement_rate_7d: float | None = Field(
        default=None,
        description=(
            "Rolling 7-day mean of this site's nightly agreement rates. "
            "``None`` when there's no history; the enforce-gate treats "
            "that as fail-closed."
        ),
    )
    experiment_url: str | None
    braintrust_logged: bool
    scorer_summaries: list[ScorerSummaryRow]


@router.get("/latest", response_model=LatestSiteReportResponse)
async def get_latest_eval(
    site: str = Query(..., description="Decision-site name."),
    _user: UserInfo = Depends(require_session),
) -> LatestSiteReportResponse:
    """Return the most recent nightly report for one site."""
    report = store.latest_report()
    if report is None:
        raise HTTPException(status_code=404, detail="no eval report has been generated yet")
    site_report = report.site(site)
    if site_report is None:
        raise HTTPException(
            status_code=404, detail=f"site {site!r} not present in the latest report"
        )
    return LatestSiteReportResponse(
        site=site_report.site,
        generated_at=report.generated_at,
        since=report.since,
        until=report.until,
        record_count=site_report.record_count,
        agreement_rate=site_report.agreement_rate(),
        agreement_rate_7d=store.rolling_agreement_rate(site),
        experiment_url=site_report.experiment_url,
        braintrust_logged=site_report.braintrust_logged,
        scorer_summaries=[
            ScorerSummaryRow(
                scorer=s.scorer,
                mean=s.mean,
                count=s.count,
                judge_disagreements=s.judge_disagreements,
            )
            for s in site_report.scorer_summaries
        ],
    )


# Re-export so the admin application can mount both this and the
# shadow router from one import site.


def augment_shadow_response(payload: dict[str, Any], *, site: str | None) -> dict[str, Any]:
    """Add ``agreement_rate_7d`` (per site) to a shadow-decisions payload.

    Extracted here so :mod:`caretaker.admin.shadow_api` can remain
    agnostic of the eval module; the admin app wires this in at route
    registration time. When ``site`` is ``None`` (all-sites query) the
    response carries a dict keyed by site, so the UI can render the
    rolling 7d number next to each decision-site heatmap bar.
    """
    if site is not None:
        payload["agreement_rate_7d"] = store.rolling_agreement_rate(site)
        return payload

    per_site: dict[str, float | None] = {}
    # Drive this from whichever sites exist in the latest report — that
    # matches the runtime universe rather than hard-coding a list that
    # can drift from ``AgenticConfig``.
    report = store.latest_report()
    if report is not None:
        for s in report.sites:
            per_site[s.site] = store.rolling_agreement_rate(s.site)
    payload["agreement_rate_7d_by_site"] = per_site
    return payload


__all__ = [
    "LatestSiteReportResponse",
    "ScorerSummaryRow",
    "augment_shadow_response",
    "router",
]
