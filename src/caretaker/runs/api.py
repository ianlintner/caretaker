"""FastAPI router for the streamed-run lifecycle.

All endpoints under ``/runs`` are authenticated via GitHub Actions OIDC
(``Authorization: Bearer <oidc_jwt>`` for ``/runs/start``) or via the
HMAC-signed ``ingest_token`` issued by ``/runs/start`` (for the
per-run streaming endpoints). Run ownership is derived from OIDC claims
— the runner cannot lie about which repo it represents.

Endpoints:

* ``POST /runs/start`` — register a run. Idempotent on the OIDC natural
  key ``(repository_id, run_id, run_attempt)``. Returns the backend
  ``run_id``, an ``ingest_token``, and the URLs the shipper should call.
* ``POST /runs/{id}/trigger`` — ask the backend to execute caretaker on
  behalf of this run. Dispatches in the background and returns 202.
* ``POST /runs/{id}/logs`` — append NDJSON log lines.
* ``POST /runs/{id}/heartbeat`` — refresh liveness.
* ``POST /runs/{id}/finish`` — terminal write with exit code + summary.
* ``GET  /runs/{id}/stream`` — SSE tail for runner-side mirror (admins
  use the ``/api/admin/runs/{id}/stream`` variant under session auth).
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse

from caretaker.auth.github_oidc import (
    ActionsPrincipal,
    require_actions_principal,
)
from caretaker.runs import tokens as ingest_tokens
from caretaker.runs.models import (
    LogEntry,
    LogStream,
    RunFinishRequest,
    RunFinishResponse,
    RunHeartbeatRequest,
    RunRecord,
    RunStartRequest,
    RunStartResponse,
    RunStatus,
    RunTriggerRequest,
    RunTriggerResponse,
    is_terminal,
)
from caretaker.runs.store import RunsStore, get_store, new_run_id
from caretaker.runs.tokens import IngestPurpose

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/runs", tags=["runs"])

# Module-level singleton so the FastAPI dependency factory is not called
# inside argument defaults (B008). Calling once at import time keeps the
# dependency identity stable for ``app.dependency_overrides`` in tests.
_REQUIRE_ACTIONS_PRINCIPAL = Depends(require_actions_principal())


try:
    _PKG_VERSION = importlib.metadata.version("caretaker-github")
except importlib.metadata.PackageNotFoundError:
    _PKG_VERSION = "0.0.0"


# ---------------------------------------------------------------------------
# Configuration plumbing — wired at startup by main.py
# ---------------------------------------------------------------------------


_dispatch_callable: Any = None  # async (RunRecord, RunTriggerRequest) -> bool


def configure_dispatch(callable_: Any) -> None:
    """Register the async function that runs agents on behalf of a run.

    Signature: ``async (RunRecord, RunTriggerRequest) -> bool`` returning
    True when dispatch was scheduled, False when it was deliberately
    skipped (e.g. App not configured / cooldown).
    """
    global _dispatch_callable  # noqa: PLW0603
    _dispatch_callable = callable_


def reset() -> None:
    """Clear configured callables (tests)."""
    global _dispatch_callable  # noqa: PLW0603
    _dispatch_callable = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _build_endpoints(request: Request, run_id: str) -> dict[str, str]:
    base = str(request.base_url).rstrip("/")
    return {
        "log_endpoint": f"{base}/runs/{run_id}/logs",
        "heartbeat_endpoint": f"{base}/runs/{run_id}/heartbeat",
        "finish_endpoint": f"{base}/runs/{run_id}/finish",
        "trigger_endpoint": f"{base}/runs/{run_id}/trigger",
        "stream_url": f"{base}/runs/{run_id}/stream",
    }


async def _emit_system_event(
    store: RunsStore,
    run_id: str,
    seq: int,
    message: str,
    **tags: Any,
) -> None:
    """Emit a backend-originated system log line into the run stream."""
    entry = LogEntry(
        seq=seq,
        ts=_utcnow(),
        stream=LogStream.SYSTEM,
        data=message,
        tags=tags,
    )
    await store.append_log(run_id, entry)


# ---------------------------------------------------------------------------
# /runs/start
# ---------------------------------------------------------------------------


@router.post("/start", response_model=RunStartResponse)
async def start_run(
    request: Request,
    body: RunStartRequest,
    principal: ActionsPrincipal = _REQUIRE_ACTIONS_PRINCIPAL,
) -> RunStartResponse:
    """Register a new run scoped by the OIDC claims.

    Idempotent on ``(repository_id, gh_run_id, gh_run_attempt)`` — the
    runner can safely retry this call.
    """
    store = get_store()
    record = RunRecord(
        run_id=new_run_id(),
        repository=principal.repository,
        repository_id=principal.repository_id,
        repository_owner=principal.repository_owner,
        gh_run_id=principal.run_id,
        gh_run_attempt=principal.run_attempt,
        actor=principal.actor,
        event_name=principal.event_name,
        workflow=principal.workflow,
        job_workflow_ref=principal.job_workflow_ref,
        sha=principal.sha,
        ref=principal.ref,
        mode=body.mode or "full",
        config_digest=body.config_digest,
        caretaker_version=body.caretaker_version or _PKG_VERSION,
        status=RunStatus.PENDING,
        started_at=_utcnow(),
    )

    persisted = await store.create_run(record)

    # Emit a system event on first start only — re-register calls don't
    # re-emit (we'd double-write the same log line on retries).
    if persisted.run_id == record.run_id:
        await _emit_system_event(
            store,
            persisted.run_id,
            seq=0,
            message=f"run started repo={persisted.repository} mode={persisted.mode}",
            actor=persisted.actor,
            event_name=persisted.event_name,
            workflow=persisted.workflow,
        )
        logger.info(
            "run.start run_id=%s repo=%s gh_run=%d/%d actor=%s",
            persisted.run_id,
            persisted.repository,
            persisted.gh_run_id,
            persisted.gh_run_attempt,
            persisted.actor,
        )
    else:
        logger.info(
            "run.start (idempotent) run_id=%s repo=%s gh_run=%d/%d",
            persisted.run_id,
            persisted.repository,
            persisted.gh_run_id,
            persisted.gh_run_attempt,
        )

    token = ingest_tokens.issue(
        run_id=persisted.run_id,
        purpose=IngestPurpose.ANY,
    )
    endpoints = _build_endpoints(request, persisted.run_id)

    return RunStartResponse(
        run_id=persisted.run_id,
        ingest_token=token,
        last_accepted_seq=persisted.last_seq,
        **endpoints,
    )


# ---------------------------------------------------------------------------
# /runs/{run_id}/trigger
# ---------------------------------------------------------------------------


@router.post("/{run_id}/trigger", response_model=RunTriggerResponse)
async def trigger_run(
    request: Request,
    body: RunTriggerRequest,
    run_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> RunTriggerResponse:
    """Ask the backend to execute caretaker agents on this run.

    Expects an ``ingest_token`` Authorization header bound to ``run_id``
    (issued by ``/runs/start``). Dispatch happens in the background;
    this endpoint returns 202 immediately and the agents stream into
    ``runs:{id}:stream`` via the dispatcher's log sink.
    """
    ingest_tokens.require_ingest_token(
        authorization=authorization,
        run_id=run_id,
        purpose=IngestPurpose.ANY,
    )

    store = get_store()
    record = await store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")
    if is_terminal(record.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run already terminal: {record.status.value}",
        )

    if _dispatch_callable is None:
        # No backend execution configured — runs/trigger is a no-op,
        # but we still flip status to running so the runner knows the
        # backend won't be sending agent output.
        await store.update_run(run_id, status=RunStatus.RUNNING)
        await _emit_system_event(
            store,
            run_id,
            seq=record.last_seq + 1,
            message="trigger received but backend dispatch is not configured",
            mode=body.mode,
        )
        return RunTriggerResponse(
            run_id=run_id,
            status=RunStatus.RUNNING,
            dispatched=False,
        )

    # Update record fields from the trigger body before dispatch, so
    # downstream agents see the right mode/event.
    updated = await store.update_run(
        run_id,
        status=RunStatus.RUNNING,
        mode=body.mode or record.mode,
        event_name=body.event_type or record.event_name,
    )

    await _emit_system_event(
        store,
        run_id,
        seq=record.last_seq + 1,
        message=f"trigger dispatched mode={updated.mode if updated else body.mode}",
        event_type=body.event_type or "",
    )

    try:
        dispatched = await _dispatch_callable(updated or record, body)
    except Exception as exc:
        logger.exception("trigger dispatch raised for run_id=%s: %s", run_id, exc)
        await store.update_run(
            run_id,
            status=RunStatus.FAILED,
            finished_at=_utcnow(),
            exit_code=1,
            summary={"error": str(exc)[:500]},
        )
        await _emit_system_event(
            store,
            run_id,
            seq=record.last_seq + 2,
            message=f"trigger failed: {exc!r}",
        )
        raise HTTPException(status_code=500, detail="dispatch failed") from exc

    return RunTriggerResponse(
        run_id=run_id,
        status=RunStatus.RUNNING,
        dispatched=dispatched,
    )


# ---------------------------------------------------------------------------
# /runs/{run_id}/logs
# ---------------------------------------------------------------------------


_MAX_LOG_BODY_BYTES = 1_000_000  # 1 MB per request


@router.post("/{run_id}/logs")
async def post_logs(
    request: Request,
    run_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> dict[str, int]:
    """Accept NDJSON-encoded log entries (one ``LogEntry`` per line).

    Each line is parsed independently; malformed lines are skipped with
    a warning. Returns ``{accepted, duplicate, malformed}`` counts.
    """
    ingest_tokens.require_ingest_token(
        authorization=authorization,
        run_id=run_id,
        purpose=IngestPurpose.LOGS,
    )

    store = get_store()
    record = await store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")
    if is_terminal(record.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run already terminal: {record.status.value}",
        )

    body = await request.body()
    if len(body) > _MAX_LOG_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"log body exceeds {_MAX_LOG_BODY_BYTES} bytes",
        )

    accepted = 0
    duplicate = 0
    malformed = 0
    max_seq = record.last_seq

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            entry = LogEntry.model_validate(obj)
        except (json.JSONDecodeError, ValueError) as exc:
            malformed += 1
            logger.debug("log line malformed run_id=%s err=%s", run_id, exc)
            continue
        ok = await store.append_log(run_id, entry)
        if ok:
            accepted += 1
            max_seq = max(max_seq, entry.seq)
        else:
            duplicate += 1

    if max_seq > record.last_seq:
        await store.update_run(run_id, last_seq=max_seq, last_heartbeat_at=_utcnow())

    return {"accepted": accepted, "duplicate": duplicate, "malformed": malformed}


# ---------------------------------------------------------------------------
# /runs/{run_id}/heartbeat
# ---------------------------------------------------------------------------


@router.post("/{run_id}/heartbeat")
async def heartbeat(
    body: RunHeartbeatRequest,
    run_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    ingest_tokens.require_ingest_token(
        authorization=authorization,
        run_id=run_id,
        purpose=IngestPurpose.HEARTBEAT,
    )

    store = get_store()
    record = await store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")

    new_last_seq = max(record.last_seq, body.seq)
    await store.update_run(
        run_id,
        last_heartbeat_at=_utcnow(),
        last_seq=new_last_seq,
    )
    return {
        "run_id": run_id,
        "last_seq": new_last_seq,
        "status": record.status.value,
    }


# ---------------------------------------------------------------------------
# /runs/{run_id}/finish
# ---------------------------------------------------------------------------


@router.post("/{run_id}/finish", response_model=RunFinishResponse)
async def finish_run(
    body: RunFinishRequest,
    run_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> RunFinishResponse:
    """Mark a run terminal. Idempotent: re-finishing a terminal run
    returns its current status without modifying it."""
    ingest_tokens.require_ingest_token(
        authorization=authorization,
        run_id=run_id,
        purpose=IngestPurpose.FINISH,
    )

    store = get_store()
    record = await store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")

    if is_terminal(record.status):
        return RunFinishResponse(run_id=run_id, status=record.status)

    new_status = RunStatus.SUCCEEDED if body.exit_code == 0 else RunStatus.FAILED
    finished_at = _utcnow()

    await store.update_run(
        run_id,
        status=new_status,
        finished_at=finished_at,
        exit_code=body.exit_code,
        summary=body.summary or {},
        report_json=body.report_json,
    )

    await _emit_system_event(
        store,
        run_id,
        seq=record.last_seq + 1,
        message=f"run finished status={new_status.value} exit_code={body.exit_code}",
        exit_code=body.exit_code,
    )

    logger.info(
        "run.finish run_id=%s status=%s exit_code=%d duration_s=%.1f",
        run_id,
        new_status.value,
        body.exit_code,
        (finished_at - record.started_at).total_seconds(),
    )
    return RunFinishResponse(run_id=run_id, status=new_status)


# ---------------------------------------------------------------------------
# /runs/{run_id}/stream — SSE tail for runner-side mirror
# ---------------------------------------------------------------------------


_SSE_PING_INTERVAL_S = 15.0


def _sse_format(*, event: str | None, data: str, event_id: str | None = None) -> bytes:
    """Encode an SSE message as bytes."""
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    # SSE requires each ``data:`` field on its own line if multi-line.
    for chunk in data.splitlines() or [""]:
        lines.append(f"data: {chunk}")
    lines.append("")  # message terminator
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _sse_iterator(
    request: Request,
    run_id: str,
    last_event_id: str | None,
) -> AsyncIterator[bytes]:
    """Async iterator yielding SSE-encoded bytes for a run's log stream."""
    store = get_store()

    # Replay history strictly newer than last_event_id (the SSE id is the
    # entry's ``seq``; clients persist this across reconnects).
    after_seq = 0
    if last_event_id:
        try:
            after_seq = int(last_event_id)
        except ValueError:
            after_seq = 0

    history = await store.read_history(run_id, after_seq=after_seq, limit=5000)
    last_stream_id = history[-1][0] if history else "$"

    for _stream_id, entry in history:
        if await request.is_disconnected():
            return
        yield _sse_format(
            event="log",
            data=entry.model_dump_json(),
            event_id=str(entry.seq),
        )

    # Live tail.
    record = await store.get_run(run_id)
    if record is not None and is_terminal(record.status):
        yield _sse_format(event="end", data=record.status.value)
        return

    async for item in store.tail(run_id, last_stream_id=last_stream_id):
        if await request.is_disconnected():
            return
        if item is None:
            yield b": ping\n\n"
            # Check terminal status so we don't tail forever.
            record = await store.get_run(run_id)
            if record is not None and is_terminal(record.status):
                yield _sse_format(event="end", data=record.status.value)
                return
            continue
        _stream_id, entry = item
        yield _sse_format(
            event="log",
            data=entry.model_dump_json(),
            event_id=str(entry.seq),
        )


@router.get("/{run_id}/stream")
async def stream_run_logs(
    request: Request,
    run_id: str = Path(...),
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-Sent Events stream of the run's log entries.

    Authorization: ``Bearer <ingest_token>`` issued by ``/runs/start``.
    Honors ``Last-Event-ID`` for resumable reconnects.
    """
    ingest_tokens.require_ingest_token(
        authorization=authorization,
        run_id=run_id,
        purpose=IngestPurpose.TAIL,
    )
    store = get_store()
    record = await store.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown run")

    return StreamingResponse(
        _sse_iterator(request, run_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


__all__ = ["configure_dispatch", "reset", "router"]
