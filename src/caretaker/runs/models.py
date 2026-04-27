"""Pydantic models for the streamed-run lifecycle.

A *run* is the backend's record of a single consumer-workflow invocation
(or, in webhook-driven mode, a single backend-initiated agent execution).
Logs are streamed into a Redis stream keyed by ``run_id`` and archived to
Mongo on terminal state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    """Lifecycle of a streamed run."""

    PENDING = "pending"  # /runs/start accepted, no work scheduled yet
    RUNNING = "running"  # /runs/{id}/trigger dispatched; agents executing
    SUCCEEDED = "succeeded"  # /runs/{id}/finish with exit_code == 0
    FAILED = "failed"  # /runs/{id}/finish with exit_code != 0
    STALLED = "stalled"  # heartbeat lapsed; sweeper marked dead
    CANCELLED = "cancelled"  # explicit cancel (admin)


_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STALLED, RunStatus.CANCELLED}
)


def is_terminal(status: RunStatus) -> bool:
    return status in _TERMINAL_STATUSES


class LogStream(StrEnum):
    """Log line origin labels — bounded for stable Prometheus cardinality."""

    STDOUT = "stdout"
    STDERR = "stderr"
    EVENT = "event"  # structured agent event (RunSummary, state change, …)
    SYSTEM = "system"  # backend-emitted (run started, run finished, …)


class LogEntry(BaseModel):
    """One streamed log line.

    Sent by the runner-side shipper in NDJSON form (one JSON object per
    line), or emitted by the backend's agent runner via the dispatcher
    log sink. ``seq`` is monotonically increasing per run and is the dedup
    key — replay/retries are safe because the backend rejects ``seq <=
    cursor`` already accepted.
    """

    seq: int = Field(..., ge=0, description="Monotonic sequence per run")
    ts: datetime = Field(default_factory=_utcnow)
    stream: LogStream = LogStream.STDOUT
    data: str = ""
    tags: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    """Backend record of a single workflow run.

    The natural key is ``(repository_id, gh_run_id, gh_run_attempt)`` from
    the GitHub OIDC claim — that lets the backend dedup the runner-side
    POST /runs/start (e.g. on retry) idempotently.
    """

    run_id: str  # backend-assigned UUID
    repository: str  # "owner/repo"
    repository_id: int
    repository_owner: str
    gh_run_id: int
    gh_run_attempt: int
    actor: str = ""
    event_name: str = ""
    workflow: str = ""
    job_workflow_ref: str = ""
    sha: str = ""
    ref: str = ""
    mode: str = "full"
    config_digest: str = ""
    caretaker_version: str = ""

    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    last_seq: int = 0
    last_heartbeat_at: datetime | None = None
    exit_code: int | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    report_json: dict[str, Any] | None = None


class RunStartRequest(BaseModel):
    mode: str = "full"
    event_type: str | None = None
    config_digest: str = ""
    caretaker_version: str = ""


class RunStartResponse(BaseModel):
    run_id: str
    ingest_token: str
    stream_url: str
    log_endpoint: str
    heartbeat_endpoint: str
    finish_endpoint: str
    trigger_endpoint: str
    last_accepted_seq: int = 0


class RunTriggerRequest(BaseModel):
    mode: str = "full"
    event_type: str | None = None
    event_payload: dict[str, Any] = Field(default_factory=dict)


class RunTriggerResponse(BaseModel):
    run_id: str
    status: RunStatus
    dispatched: bool


class RunHeartbeatRequest(BaseModel):
    seq: int = Field(0, ge=0)


class RunFinishRequest(BaseModel):
    exit_code: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)
    report_json: dict[str, Any] | None = None


class RunFinishResponse(BaseModel):
    run_id: str
    status: RunStatus


class RunSummaryView(BaseModel):
    """Light-weight projection used by admin list/detail endpoints."""

    run_id: str
    repository: str
    actor: str
    event_name: str
    mode: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    last_seq: int = 0
    last_heartbeat_at: datetime | None = None
    workflow: str = ""
    sha: str = ""


__all__ = [
    "LogEntry",
    "LogStream",
    "RunFinishRequest",
    "RunFinishResponse",
    "RunHeartbeatRequest",
    "RunRecord",
    "RunStartRequest",
    "RunStartResponse",
    "RunStatus",
    "RunSummaryView",
    "RunTriggerRequest",
    "RunTriggerResponse",
    "is_terminal",
]
