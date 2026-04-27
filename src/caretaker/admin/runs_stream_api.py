"""Admin-side runs API: list, detail, and SSE log stream.

The SSE endpoint here mirrors the runner-side ``/runs/{id}/stream`` but
authenticates via the admin dashboard's OIDC session cookie instead of
an ``ingest_token``. The two endpoints share an SSE iterator factory so
the wire format is identical.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse

from caretaker.admin.auth import UserInfo, require_session
from caretaker.runs.api import _sse_iterator
from caretaker.runs.models import RunStatus, RunSummaryView, is_terminal
from caretaker.runs.store import get_store

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/admin/runs", tags=["admin", "runs"])


@router.get("")
async def list_runs(
    repo: str | None = Query(default=None, description="Filter by owner/repo"),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    since_minutes: int | None = Query(
        default=None,
        ge=1,
        le=60 * 24 * 30,
        description="Only return runs started within the last N minutes",
    ),
    _user: UserInfo = Depends(require_session),
) -> list[RunSummaryView]:
    store = get_store()
    parsed_status: RunStatus | None = None
    if status_filter:
        try:
            parsed_status = RunStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown status filter: {status_filter}",
            ) from exc
    since: datetime | None = None
    if since_minutes:
        since = datetime.now(UTC) - timedelta(minutes=since_minutes)
    return await store.list_runs(
        repository=repo,
        status=parsed_status,
        since=since,
        limit=limit,
    )


@router.get("/{run_id}")
async def get_run(
    run_id: str = Path(...),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    record = await get_store().get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")
    return record.model_dump(mode="json")


@router.get("/{run_id}/stream")
async def stream_run(
    request: Request,
    run_id: str = Path(...),
    _user: UserInfo = Depends(require_session),
) -> StreamingResponse:
    """Server-Sent Events feed of a run's live logs (admin-authenticated)."""
    record = await get_store().get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")

    last_event_id = request.headers.get("Last-Event-ID")
    return StreamingResponse(
        _sse_iterator(request, run_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Stalled-run sweeper — flips silent runs to ``stalled``
# ---------------------------------------------------------------------------


_DEFAULT_STALL_SECONDS = 5 * 60


async def sweep_stalled_runs(*, max_silent_seconds: int = _DEFAULT_STALL_SECONDS) -> int:
    """One sweep pass: flip non-terminal runs with no recent heartbeat to ``stalled``.

    Returns the count of runs marked stalled. Designed to be called from
    a long-lived background task in the FastAPI lifespan.
    """
    store = get_store()
    now = datetime.now(UTC)
    threshold = now - timedelta(seconds=max_silent_seconds)
    runs = await store.list_runs(status=RunStatus.RUNNING, limit=500)
    stalled = 0
    for view in runs:
        if is_terminal(view.status):
            continue
        last = view.last_heartbeat_at or view.started_at
        if last < threshold:
            await store.update_run(
                view.run_id,
                status=RunStatus.STALLED,
                finished_at=now,
            )
            logger.warning(
                "run.sweep stalled run_id=%s repo=%s last_heartbeat=%s",
                view.run_id,
                view.repository,
                last.isoformat(),
            )
            stalled += 1
    # Same pass for PENDING runs that never got a /trigger.
    pending = await store.list_runs(status=RunStatus.PENDING, limit=500)
    for view in pending:
        last = view.last_heartbeat_at or view.started_at
        if last < threshold:
            await store.update_run(
                view.run_id,
                status=RunStatus.STALLED,
                finished_at=now,
            )
            stalled += 1
    return stalled


def build_sweeper_task(interval_seconds: float = 60.0) -> asyncio.Task[None]:
    """Background task that sweeps stalled runs every ``interval_seconds``."""

    async def _loop() -> None:
        while True:
            try:
                await sweep_stalled_runs()
            except Exception:  # pragma: no cover — defensive
                logger.exception("run sweeper iteration failed")
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_loop(), name="runs-sweeper")


__all__ = ["build_sweeper_task", "router", "sweep_stalled_runs"]
