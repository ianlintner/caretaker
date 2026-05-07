from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    SPAWNING = "SPAWNING"
    STARTED = "STARTED"
    CLONING = "CLONING"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"
    DEAD_LETTER = "DEAD_LETTER"


def make_job_id(repo: str, task_type: str, base_sha: str, instructions: str) -> str:
    """SHA-256 content-addressed 16-char hex ID. Same inputs → same ID."""
    payload = json.dumps(
        {
            "repo": repo,
            "task_type": task_type,
            "base_sha": base_sha,
            "instructions": instructions.strip().lower(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def k8s_job_name(job_id: str, attempt: int) -> str:
    """DNS-safe K8s Job name, always ≤ 63 chars."""
    return f"ct-coding-{job_id}-a{attempt}"


@dataclass(frozen=True)
class CodingJobMessage:
    """Task specification. Attempt and lineage come from ASB system properties."""

    job_id: str
    repo: str
    task_type: str
    base_sha: str
    instructions: str
    context: str = ""
    first_enqueued_ts: float = field(default_factory=time.time)
    attempt: int = 1  # populated from ASB delivery_count on receive; 1 on first send

    def to_asb_body(self) -> dict[str, Any]:
        """Minimal JSON body — task spec only. No lineage or tracing."""
        body: dict[str, Any] = {
            "job_id": self.job_id,
            "repo": self.repo,
            "task_type": self.task_type,
            "base_sha": self.base_sha,
            "instructions": self.instructions,
        }
        if self.context:
            body["context"] = self.context
        return body

    def to_asb_properties(self, traceparent: str = "") -> dict[str, str]:
        """application_properties: routing + tracing. No body parse needed for routing."""
        return {
            "job_id": self.job_id,
            "first_enqueued_ts": str(self.first_enqueued_ts),
            "attempt": str(self.attempt),
            "traceparent": traceparent,
        }

    def asb_message_id(self) -> str:
        """Stable per-attempt message ID for observability."""
        return f"ct-{self.job_id}-a{self.attempt}"

    @classmethod
    def from_asb(
        cls,
        *,
        body: dict[str, Any],
        properties: dict[str, Any],
        delivery_count: int,
    ) -> CodingJobMessage:
        # For scheduled retries (delivery_count=0), use attempt from properties if present
        attempt_from_props = int(properties.get("attempt", "1"))
        attempt = attempt_from_props if delivery_count == 0 else delivery_count + 1
        return cls(
            job_id=body["job_id"],
            repo=body["repo"],
            task_type=body["task_type"],
            base_sha=body["base_sha"],
            instructions=body["instructions"],
            context=body.get("context", ""),
            first_enqueued_ts=float(properties.get("first_enqueued_ts", time.time())),
            attempt=attempt,
        )

    @property
    def k8s_name(self) -> str:
        return k8s_job_name(self.job_id, self.attempt)


@dataclass
class StatusEvent:
    job_id: str
    status: JobStatus
    attempt: int
    phase: str = ""
    heartbeat_ts: float = field(default_factory=time.time)
    progress: str = ""
    result_patch: str = ""
    commit_sha: str = ""
    pr_url: str = ""
    error: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "phase": self.phase,
            "heartbeat_ts": self.heartbeat_ts,
            "progress": self.progress,
            "result_patch": self.result_patch,
            "commit_sha": self.commit_sha,
            "pr_url": self.pr_url,
            "error": self.error,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StatusEvent:
        return cls(
            job_id=payload["job_id"],
            status=JobStatus(payload["status"]),
            attempt=int(payload.get("attempt", 1)),
            phase=payload.get("phase", ""),
            heartbeat_ts=float(payload.get("heartbeat_ts", 0.0)),
            progress=payload.get("progress", ""),
            result_patch=payload.get("result_patch", ""),
            commit_sha=payload.get("commit_sha", ""),
            pr_url=payload.get("pr_url", ""),
            error=payload.get("error", ""),
        )


__all__ = [
    "CodingJobMessage",
    "JobStatus",
    "StatusEvent",
    "k8s_job_name",
    "make_job_id",
]
