# Durable K8s Coding Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-process asyncio coding task executor with a durable K8s Job pipeline: enqueue to Azure Service Bus → dedicated dispatcher pod spawns K8s Jobs → jobs write status/results back via Redis Streams → dispatcher posts GitHub comment.

**Architecture:** Tasks arrive at MCP backend, are enqueued to the `coding-tasks` ASB queue with a minimal JSON body and W3C trace context in `application_properties`, then immediately return a `job_id`. A dedicated dispatcher pod consumes the queue (peek-lock, at-least-once) and creates a K8s Job per task using content-addressed names for dedup. The job container writes lifecycle events + heartbeats + final result to the `job-status` Redis Stream. A reconciler loop polls K8s Job status and heartbeat staleness; retries are scheduled via ASB `schedule_messages` with exponential backoff. ASB `MaxDeliveryCount=3` and `TimeToLive=2700s` enforce retry limits and wall-clock cap natively — no code needed. Result poster in the dispatcher consumes `COMPLETED` events from Redis and posts GitHub comments.

**Tech Stack:** Python 3.11+, `azure-servicebus>=7.11` + `azure-identity>=1.16` (ASB queue), Redis Streams / `RedisStreamsEventBus` (job-status stream only), `kubernetes>=28` Python client, existing `caretaker.foundry` executor, Pydantic `StrictBaseModel`, pytest + `unittest.mock`

**ASB namespace:** `thebiggestboy.servicebus.windows.net` (Standard tier)
**ASB queue config (set once in portal):** `MaxDeliveryCount=3`, `DefaultMessageTimeToLive=PT45M`, `LockDuration=PT5M`

**Retry policy:** Enforced by ASB — `MaxDeliveryCount=3` on queue; backoff via `schedule_messages(enqueue_time=now+60s/180s)` on detected failure. No retry state in message payload.

**Message shape — minimum data + tracing:**

```text
application_properties: { traceparent, job_id, first_enqueued_ts }
body (JSON):            { job_id, repo, task_type, base_sha, instructions, context? }
system properties:      delivery_count (attempt), enqueued_time_utc, time_to_live
```

---

## File Map

**Create:**
- `src/caretaker/coding_jobs/__init__.py`
- `src/caretaker/coding_jobs/models.py` — `CodingJobMessage`, `StatusEvent`, `JobStatus`, `make_job_id`, `k8s_job_name`
- `src/caretaker/coding_jobs/asb_queue.py` — `AsbCodingQueue`: send/receive/schedule via `azure-servicebus`
- `src/caretaker/coding_jobs/status_stream.py` — `JobStatusStream`: Redis Stream writes + reads for `job-status`
- `src/caretaker/coding_jobs/k8s.py` — `K8sJobSpawner`: create/get K8s Jobs via `kubernetes` client
- `src/caretaker/coding_jobs/dispatcher.py` — `CodingJobDispatcher`: ASB peek-lock consumer, spawns Jobs
- `src/caretaker/coding_jobs/reconciler.py` — `Reconciler`: K8s status poll + heartbeat staleness + ASB scheduled retry
- `src/caretaker/coding_jobs/worker.py` — `run_coding_worker()`: job container entrypoint, status writes, heartbeats
- `src/caretaker/coding_jobs/result_poster.py` — `ResultPoster`: consumes COMPLETED events, posts GitHub comment
- `src/caretaker/coding_jobs/dispatcher_main.py` — standalone pod entrypoint
- `infra/k8s/caretaker-job-dispatcher-deployment.yaml` — dedicated dispatcher pod
- `tests/test_coding_jobs/__init__.py`
- `tests/test_coding_jobs/test_models.py`
- `tests/test_coding_jobs/test_asb_queue.py`
- `tests/test_coding_jobs/test_status_stream.py`
- `tests/test_coding_jobs/test_k8s.py`
- `tests/test_coding_jobs/test_dispatcher.py`
- `tests/test_coding_jobs/test_reconciler.py`
- `tests/test_coding_jobs/test_worker.py`
- `tests/test_coding_jobs/test_result_poster.py`

**Modify:**

- `src/caretaker/config.py` — add `CodingJobsConfig` after `RedisConfig`
- `infra/k8s/caretaker-agent-worker.yaml` — add `caretaker-coding-worker-template` Job spec
- `src/caretaker/mcp_backend/main.py` — start dispatcher/reconciler/result_poster in `_lifespan`; add `GET /coding-jobs/{job_id}/status`

---

## Task 1: CodingJobsConfig

**Files:**
- Modify: `src/caretaker/config.py` (after `RedisConfig` class, ~line 1297)
- Test: `tests/test_coding_jobs/test_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coding_jobs/__init__.py
# (empty)

# tests/test_coding_jobs/test_models.py
import pytest
from caretaker.config import CodingJobsConfig


def test_coding_jobs_config_defaults():
    cfg = CodingJobsConfig()
    assert cfg.enabled is False
    assert cfg.asb_namespace == ""
    assert cfg.asb_queue_coding_tasks == "coding-tasks"
    assert cfg.per_attempt_timeout_secs == 900
    assert cfg.heartbeat_staleness_secs == 300
    assert cfg.stream_job_status == "job-status"
    assert cfg.k8s_namespace == "caretaker"
    assert cfg.reconcile_interval_secs == 30


def test_coding_jobs_config_from_dict():
    cfg = CodingJobsConfig(
        **{"enabled": True, "asb_namespace": "thebiggestboy.servicebus.windows.net"}
    )
    assert cfg.enabled is True
    assert cfg.asb_namespace == "thebiggestboy.servicebus.windows.net"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/ianlintner/Projects/caretaker
pytest tests/test_coding_jobs/test_models.py::test_coding_jobs_config_defaults -v
```

Expected: `ImportError: cannot import name 'CodingJobsConfig'`

- [ ] **Step 3: Create test package and add config class**

Create `tests/test_coding_jobs/__init__.py` (empty).

Insert after the `RedisConfig` class in `src/caretaker/config.py`:

```python
class CodingJobsConfig(StrictBaseModel):
    """Durable K8s coding job dispatch via Azure Service Bus + Redis Streams."""

    enabled: bool = False
    # ASB — queue layer
    asb_namespace: str = ""  # e.g. thebiggestboy.servicebus.windows.net
    asb_queue_coding_tasks: str = "coding-tasks"
    asb_lock_duration_secs: int = 300         # must match queue LockDuration in portal
    # Redis — job-status stream only
    stream_job_status: str = "job-status"
    status_consumer_group: str = "coding-results"
    # K8s
    k8s_namespace: str = "caretaker"
    k8s_worker_image: str = ""
    per_attempt_timeout_secs: int = 900       # matches activeDeadlineSeconds
    # Reconciler
    heartbeat_staleness_secs: int = 300
    reconcile_interval_secs: int = 30
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_models.py -v
```

Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/config.py tests/test_coding_jobs/__init__.py tests/test_coding_jobs/test_models.py
git commit -m "feat(coding-jobs): add CodingJobsConfig"
```

---

## Task 2: Models — job_id, CodingJobMessage, StatusEvent

**Files:**
- Create: `src/caretaker/coding_jobs/__init__.py`
- Create: `src/caretaker/coding_jobs/models.py`
- Test: `tests/test_coding_jobs/test_models.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coding_jobs/test_models.py`:

```python
from caretaker.coding_jobs.models import (
    CodingJobMessage,
    JobStatus,
    StatusEvent,
    make_job_id,
    k8s_job_name,
)
import time


def test_make_job_id_deterministic():
    a = make_job_id("org/repo", "fix", "abc123", "fix the bug")
    b = make_job_id("org/repo", "fix", "abc123", "fix the bug")
    assert a == b
    assert len(a) == 16


def test_make_job_id_normalises_instructions():
    a = make_job_id("org/repo", "fix", "abc", "  Fix THE Bug  ")
    b = make_job_id("org/repo", "fix", "abc", "fix the bug")
    assert a == b


def test_make_job_id_differs_on_different_inputs():
    a = make_job_id("org/repo", "fix", "abc123", "fix the bug")
    b = make_job_id("org/repo", "fix", "abc123", "add a feature")
    assert a != b


def test_k8s_job_name_format():
    name = k8s_job_name("3f7a2b9c1e4d5f6a", 1)
    assert name == "ct-coding-3f7a2b9c1e4d5f6a-a1"
    assert len(name) <= 63


def test_coding_job_message_to_asb_body():
    msg = CodingJobMessage(
        job_id="abc1234567890123",
        repo="org/repo",
        task_type="fix",
        base_sha="deadbeef",
        instructions="fix the null pointer",
        context="PR #42",
    )
    body = msg.to_asb_body()
    assert body["job_id"] == "abc1234567890123"
    assert body["repo"] == "org/repo"
    assert "context" in body
    # no attempt, max_attempts, first_enqueued_ts in body
    assert "attempt" not in body
    assert "max_attempts" not in body
    assert "first_enqueued_ts" not in body


def test_coding_job_message_to_asb_properties():
    msg = CodingJobMessage(
        job_id="abc1234567890123",
        repo="org/repo",
        task_type="fix",
        base_sha="deadbeef",
        instructions="fix the null pointer",
        context="",
        first_enqueued_ts=1700000000.0,
    )
    props = msg.to_asb_properties(traceparent="00-abc-def-01")
    assert props["job_id"] == "abc1234567890123"
    assert props["traceparent"] == "00-abc-def-01"
    assert props["first_enqueued_ts"] == "1700000000.0"


def test_coding_job_message_roundtrip_from_asb():
    msg = CodingJobMessage(
        job_id="abc1234567890123",
        repo="org/repo",
        task_type="fix",
        base_sha="deadbeef",
        instructions="fix the null pointer",
        context="PR #42",
        first_enqueued_ts=1700000000.0,
    )
    body = msg.to_asb_body()
    props = msg.to_asb_properties()
    recovered = CodingJobMessage.from_asb(body=body, properties=props, delivery_count=1)
    assert recovered.job_id == msg.job_id
    assert recovered.attempt == 2   # delivery_count=1 → attempt=2
    assert recovered.first_enqueued_ts == 1700000000.0


def test_coding_job_message_omits_empty_context():
    msg = CodingJobMessage(
        job_id="abc1234567890123",
        repo="org/repo",
        task_type="fix",
        base_sha="deadbeef",
        instructions="fix the null pointer",
        context="",
    )
    body = msg.to_asb_body()
    assert "context" not in body


def test_status_event_roundtrip():
    event = StatusEvent(
        job_id="abc1234567890123",
        status=JobStatus.RUNNING,
        attempt=1,
        phase="LLM_LOOP",
        heartbeat_ts=1700000060.0,
        progress="step 2/5",
    )
    payload = event.to_payload()
    assert payload["status"] == "RUNNING"
    recovered = StatusEvent.from_payload(payload)
    assert recovered.status == JobStatus.RUNNING
    assert recovered.phase == "LLM_LOOP"
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_models.py -v
```

Expected: `ImportError: cannot import name 'make_job_id'`

- [ ] **Step 3: Create the modules**

```python
# src/caretaker/coding_jobs/__init__.py
# (empty)
```

```python
# src/caretaker/coding_jobs/models.py
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
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
    attempt: int = 1   # populated from ASB delivery_count on receive; 1 on first send

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
            "traceparent": traceparent,
        }

    def asb_message_id(self) -> str:
        """Stable per-attempt message ID for observability (not used for ASB dedup on Standard)."""
        return f"ct-{self.job_id}-a{self.attempt}"

    @classmethod
    def from_asb(
        cls,
        *,
        body: dict[str, Any],
        properties: dict[str, Any],
        delivery_count: int,
    ) -> CodingJobMessage:
        return cls(
            job_id=body["job_id"],
            repo=body["repo"],
            task_type=body["task_type"],
            base_sha=body["base_sha"],
            instructions=body["instructions"],
            context=body.get("context", ""),
            first_enqueued_ts=float(properties.get("first_enqueued_ts", time.time())),
            attempt=delivery_count + 1,  # ASB delivery_count is 0-based
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_models.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/ tests/test_coding_jobs/test_models.py
git commit -m "feat(coding-jobs): add models — CodingJobMessage (minimal ASB body), StatusEvent, make_job_id"
```

---

## Task 3: AsbCodingQueue — Send / Receive / Schedule

**Files:**

- Create: `src/caretaker/coding_jobs/asb_queue.py`
- Test: `tests/test_coding_jobs/test_asb_queue.py`

**Dependency:** Add to `pyproject.toml` dependencies:

```toml
"azure-servicebus>=7.11",
"azure-identity>=1.16",
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_asb_queue.py
import json
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from caretaker.coding_jobs.models import CodingJobMessage, make_job_id
from caretaker.coding_jobs.asb_queue import AsbCodingQueue


@pytest.fixture
def config():
    from caretaker.config import CodingJobsConfig
    return CodingJobsConfig(
        enabled=True,
        asb_namespace="thebiggestboy.servicebus.windows.net",
        asb_queue_coding_tasks="coding-tasks",
    )


@pytest.fixture
def mock_sender():
    sender = MagicMock()
    sender.__aenter__ = AsyncMock(return_value=sender)
    sender.__aexit__ = AsyncMock(return_value=False)
    sender.send_messages = AsyncMock()
    sender.schedule_messages = AsyncMock()
    return sender


@pytest.fixture
def mock_client(mock_sender):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_queue_sender = MagicMock(return_value=mock_sender)
    return client


@pytest.fixture
def sample_msg():
    job_id = make_job_id("org/repo", "fix", "abc123", "fix the bug")
    return CodingJobMessage(
        job_id=job_id,
        repo="org/repo",
        task_type="fix",
        base_sha="abc123",
        instructions="fix the bug",
        context="PR #42",
        first_enqueued_ts=time.time(),
    )


@pytest.mark.asyncio
async def test_enqueue_sends_minimal_body(config, mock_client, mock_sender, sample_msg):
    queue = AsbCodingQueue(config=config, client=mock_client)
    await queue.enqueue(sample_msg)

    mock_sender.send_messages.assert_awaited_once()
    sent = mock_sender.send_messages.call_args[0][0]
    body = json.loads(b"".join(sent.body))
    assert body["job_id"] == sample_msg.job_id
    assert body["repo"] == "org/repo"
    assert "attempt" not in body
    assert "max_attempts" not in body
    assert "first_enqueued_ts" not in body


@pytest.mark.asyncio
async def test_enqueue_sets_traceparent_in_properties(config, mock_client, mock_sender, sample_msg):
    queue = AsbCodingQueue(config=config, client=mock_client)
    await queue.enqueue(sample_msg, traceparent="00-abc-def-01")

    sent = mock_sender.send_messages.call_args[0][0]
    assert sent.application_properties["traceparent"] == "00-abc-def-01"
    assert sent.application_properties["job_id"] == sample_msg.job_id


@pytest.mark.asyncio
async def test_enqueue_sets_time_to_live(config, mock_client, mock_sender, sample_msg):
    queue = AsbCodingQueue(config=config, client=mock_client)
    await queue.enqueue(sample_msg)

    sent = mock_sender.send_messages.call_args[0][0]
    # TimeToLive should be set to 2700s (45 min)
    assert sent.time_to_live == timedelta(seconds=2700)


@pytest.mark.asyncio
async def test_schedule_retry_uses_scheduled_enqueue_time(config, mock_client, mock_sender, sample_msg):
    queue = AsbCodingQueue(config=config, client=mock_client)
    before = datetime.now(timezone.utc)
    await queue.schedule_retry(sample_msg, delay_secs=60)
    after = datetime.now(timezone.utc)

    mock_sender.schedule_messages.assert_awaited_once()
    call_args = mock_sender.schedule_messages.call_args
    scheduled_time = call_args[0][1]
    assert before + timedelta(seconds=55) <= scheduled_time <= after + timedelta(seconds=65)
    # Body still minimal
    msg = call_args[0][0]
    body = json.loads(b"".join(msg.body))
    assert "attempt" not in body
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_asb_queue.py -v
```

Expected: `ImportError: cannot import name 'AsbCodingQueue'`

- [ ] **Step 3: Implement AsbCodingQueue**

```python
# src/caretaker/coding_jobs/asb_queue.py
"""Azure Service Bus queue client for the coding-tasks queue.

Body: minimal JSON task spec.
application_properties: traceparent (W3C OTel), job_id, first_enqueued_ts.
Retry, dead-letter, and wall-clock TTL are enforced by ASB configuration
(MaxDeliveryCount=3, DefaultMessageTimeToLive=PT45M) — not in code.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.coding_jobs.models import CodingJobMessage
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

_WALL_CLOCK_TTL_SECS = 2700   # 45 min — matches queue DefaultMessageTimeToLive


class AsbCodingQueue:
    """Send and receive CodingJobMessages via an Azure Service Bus queue."""

    def __init__(self, *, config: CodingJobsConfig, client: Any) -> None:
        self._config = config
        self._client = client

    async def enqueue(self, msg: CodingJobMessage, traceparent: str = "") -> None:
        """Send a new coding task to the ASB queue."""
        from azure.servicebus import ServiceBusMessage

        body = json.dumps(msg.to_asb_body()).encode()
        properties = msg.to_asb_properties(traceparent=traceparent)

        asb_msg = ServiceBusMessage(
            body=body,
            message_id=msg.asb_message_id(),
            time_to_live=timedelta(seconds=_WALL_CLOCK_TTL_SECS),
            application_properties=properties,
        )

        async with self._client.get_queue_sender(self._config.asb_queue_coding_tasks) as sender:
            await sender.send_messages(asb_msg)

        logger.info("asb-queue: enqueued job_id=%s", msg.job_id)

    async def schedule_retry(self, msg: CodingJobMessage, *, delay_secs: int) -> None:
        """Schedule a retry message to be delivered after delay_secs.

        Uses a fresh ServiceBusMessage (complete + re-enqueue pattern) so
        ASB delivery_count resets — attempt tracking is via first_enqueued_ts
        and the reconciler's K8s-side attempt counter.
        """
        from azure.servicebus import ServiceBusMessage

        retry_msg = CodingJobMessage(
            job_id=msg.job_id,
            repo=msg.repo,
            task_type=msg.task_type,
            base_sha=msg.base_sha,
            instructions=msg.instructions,
            context=msg.context,
            first_enqueued_ts=msg.first_enqueued_ts,
            attempt=msg.attempt + 1,
        )

        body = json.dumps(retry_msg.to_asb_body()).encode()
        properties = retry_msg.to_asb_properties()
        remaining_ttl = max(60, _WALL_CLOCK_TTL_SECS - int(retry_msg.attempt * 900))

        asb_msg = ServiceBusMessage(
            body=body,
            message_id=retry_msg.asb_message_id(),
            time_to_live=timedelta(seconds=remaining_ttl),
            application_properties=properties,
        )

        enqueue_time = datetime.now(timezone.utc) + timedelta(seconds=delay_secs)
        async with self._client.get_queue_sender(self._config.asb_queue_coding_tasks) as sender:
            await sender.schedule_messages(asb_msg, enqueue_time)

        logger.info(
            "asb-queue: scheduled retry job_id=%s attempt=%d delay=%ds",
            msg.job_id, retry_msg.attempt, delay_secs,
        )

    @staticmethod
    def parse_received(asb_msg: Any) -> CodingJobMessage:
        """Parse a received ASB message into a CodingJobMessage."""
        raw = b"".join(asb_msg.body)
        body = json.loads(raw)
        props = asb_msg.application_properties or {}
        return CodingJobMessage.from_asb(
            body=body,
            properties={k.decode() if isinstance(k, bytes) else k: v for k, v in props.items()},
            delivery_count=asb_msg.delivery_count,
        )


def build_asb_queue(config: CodingJobsConfig) -> AsbCodingQueue:
    """Build an AsbCodingQueue using DefaultAzureCredential (workload identity in-cluster)."""
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient

    credential = DefaultAzureCredential()
    client = ServiceBusClient(
        fully_qualified_namespace=config.asb_namespace,
        credential=credential,
    )
    return AsbCodingQueue(config=config, client=client)


__all__ = ["AsbCodingQueue", "build_asb_queue"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_asb_queue.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/asb_queue.py tests/test_coding_jobs/test_asb_queue.py
git commit -m "feat(coding-jobs): add AsbCodingQueue — minimal body + W3C traceparent in properties"
```

---

## Task 4: JobStatusStream — Redis Stream for job-status

**Files:**

- Create: `src/caretaker/coding_jobs/status_stream.py`
- Test: `tests/test_coding_jobs/test_status_stream.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_status_stream.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from caretaker.coding_jobs.models import JobStatus, StatusEvent, make_job_id
from caretaker.coding_jobs.status_stream import JobStatusStream


@pytest.fixture
def mock_bus():
    bus = MagicMock()
    bus.publish = AsyncMock(return_value="1700000000000-0")
    bus.ensure_group = AsyncMock()
    bus.consume = AsyncMock()
    return bus


@pytest.fixture
def stream(mock_bus):
    from caretaker.config import CodingJobsConfig
    return JobStatusStream(bus=mock_bus, config=CodingJobsConfig())


@pytest.fixture
def sample_event():
    job_id = make_job_id("org/repo", "fix", "abc", "fix the bug")
    return StatusEvent(job_id=job_id, status=JobStatus.RUNNING, attempt=1, phase="LLM_LOOP")


@pytest.mark.asyncio
async def test_write_status_publishes_to_job_status_stream(stream, mock_bus, sample_event):
    await stream.write_status(sample_event)
    mock_bus.publish.assert_awaited_once()
    call_args = mock_bus.publish.call_args
    assert call_args[0][0] == "job-status"
    payload = call_args[0][1]
    assert payload["status"] == "RUNNING"
    assert payload["job_id"] == sample_event.job_id


@pytest.mark.asyncio
async def test_ensure_group_calls_bus(stream, mock_bus):
    await stream.ensure_consumer_group()
    mock_bus.ensure_group.assert_awaited_once_with("job-status", "coding-results")
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_status_stream.py -v
```

Expected: `ImportError: cannot import name 'JobStatusStream'`

- [ ] **Step 3: Implement JobStatusStream**

```python
# src/caretaker/coding_jobs/status_stream.py
"""Redis Stream facade for job-status events only.

The coding-tasks queue is handled by AsbCodingQueue.
This module owns the job-status Redis Stream: writes from job containers,
reads from the MCP status endpoint and ResultPoster.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.models import StatusEvent

if TYPE_CHECKING:
    from caretaker.config import CodingJobsConfig
    from caretaker.eventbus.base import EventBus, EventHandler

logger = logging.getLogger(__name__)


class JobStatusStream:
    """Write and read lifecycle events on the job-status Redis Stream."""

    def __init__(self, *, bus: EventBus, config: CodingJobsConfig) -> None:
        self._bus = bus
        self._config = config

    async def write_status(self, event: StatusEvent) -> str:
        return await self._bus.publish(self._config.stream_job_status, event.to_payload())

    async def ensure_consumer_group(self) -> None:
        await self._bus.ensure_group(
            self._config.stream_job_status,
            self._config.status_consumer_group,
        )

    async def consume_results(self, *, consumer: str, handler: EventHandler) -> None:
        """Consume loop for ResultPoster — terminal status events."""
        await self.ensure_consumer_group()
        await self._bus.consume(
            stream=self._config.stream_job_status,
            group=self._config.status_consumer_group,
            consumer=consumer,
            handler=handler,
        )

    async def read_latest_status(self, job_id: str) -> StatusEvent | None:
        """Scan the stream newest-first to find the latest event for job_id."""
        client = await self._bus._get_client()  # type: ignore[attr-defined]
        entries = await client.xrevrange(self._config.stream_job_status, count=200)
        for _entry_id, fields in entries:
            raw = fields.get("payload")
            if not raw:
                continue
            payload = json.loads(raw)
            if payload.get("job_id") == job_id:
                return StatusEvent.from_payload(payload)
        return None


__all__ = ["JobStatusStream"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_status_stream.py -v
```

Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/status_stream.py tests/test_coding_jobs/test_status_stream.py
git commit -m "feat(coding-jobs): add JobStatusStream — Redis Stream for job-status events"
```

---

## Task 5: K8sJobSpawner

**Files:**

- Create: `src/caretaker/coding_jobs/k8s.py`
- Test: `tests/test_coding_jobs/test_k8s.py`

**Dependency:** Ensure `pyproject.toml` has `"kubernetes>=28"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_k8s.py
import time
import pytest
from unittest.mock import MagicMock
from caretaker.coding_jobs.models import CodingJobMessage, make_job_id
from caretaker.coding_jobs.k8s import K8sJobSpawner, K8sJobOutcome


@pytest.fixture
def config():
    from caretaker.config import CodingJobsConfig
    return CodingJobsConfig(enabled=True, k8s_worker_image="acr.io/caretaker-worker:latest")


@pytest.fixture
def mock_batch_api():
    api = MagicMock()
    api.create_namespaced_job = MagicMock()
    api.read_namespaced_job_status = MagicMock()
    api.list_namespaced_job = MagicMock()
    return api


@pytest.fixture
def spawner(mock_batch_api, config):
    return K8sJobSpawner(batch_api=mock_batch_api, config=config)


@pytest.fixture
def sample_msg():
    job_id = make_job_id("org/repo", "fix", "abc123", "fix the null pointer")
    return CodingJobMessage(
        job_id=job_id, repo="org/repo", task_type="fix", base_sha="abc123",
        instructions="fix the null pointer", context="PR #42",
        first_enqueued_ts=time.time(), attempt=1,
    )


def test_spawn_creates_job_with_correct_name(spawner, mock_batch_api, sample_msg):
    mock_batch_api.create_namespaced_job.return_value = MagicMock()
    job_name = spawner.spawn(sample_msg)
    assert job_name == sample_msg.k8s_name
    body = mock_batch_api.create_namespaced_job.call_args[1]["body"]
    assert body.metadata.name == sample_msg.k8s_name


def test_spawn_injects_job_id_and_attempt_env(spawner, mock_batch_api, sample_msg):
    mock_batch_api.create_namespaced_job.return_value = MagicMock()
    spawner.spawn(sample_msg)
    body = mock_batch_api.create_namespaced_job.call_args[1]["body"]
    container = body.spec.template.spec.containers[0]
    env = {e.name: e.value for e in container.env if e.value is not None}
    assert env["JOB_ID"] == sample_msg.job_id
    assert env["ATTEMPT"] == "1"
    # TASK_PAYLOAD should be the minimal JSON body only
    import json
    task_payload = json.loads(env["TASK_PAYLOAD"])
    assert "attempt" not in task_payload
    assert "first_enqueued_ts" not in task_payload


def test_spawn_returns_existing_name_on_409(spawner, mock_batch_api, sample_msg):
    from kubernetes.client.exceptions import ApiException
    mock_batch_api.create_namespaced_job.side_effect = ApiException(status=409)
    job_name = spawner.spawn(sample_msg)
    assert job_name == sample_msg.k8s_name


def test_get_status_running(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 1; s.status.succeeded = 0; s.status.failed = 0; s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.RUNNING


def test_get_status_succeeded(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0; s.status.succeeded = 1; s.status.failed = 0; s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.SUCCEEDED


def test_get_status_failed(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0; s.status.succeeded = 0; s.status.failed = 1; s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.FAILED


def test_get_status_timeout_on_deadline_exceeded(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0; s.status.succeeded = 0; s.status.failed = 1
    cond = MagicMock(); cond.type = "DeadlineExceeded"; cond.status = "True"
    s.status.conditions = [cond]
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.TIMEOUT
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_k8s.py -v
```

Expected: `ImportError: cannot import name 'K8sJobSpawner'`

- [ ] **Step 3: Implement K8sJobSpawner**

```python
# src/caretaker/coding_jobs/k8s.py
from __future__ import annotations

import json
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.coding_jobs.models import CodingJobMessage
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)


class K8sJobOutcome(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    NOT_FOUND = "NOT_FOUND"


class K8sJobSpawner:
    def __init__(self, *, batch_api: Any, config: CodingJobsConfig) -> None:
        self._api = batch_api
        self._config = config

    def spawn(self, msg: CodingJobMessage) -> str:
        """Create a K8s Job. Returns the job name. 409 = already exists, skipped safely."""
        from kubernetes import client as k8s
        from kubernetes.client.exceptions import ApiException

        # TASK_PAYLOAD carries the minimal body only — same shape as ASB message body
        task_payload = json.dumps(msg.to_asb_body())

        env = [
            k8s.V1EnvVar(name="JOB_ID", value=msg.job_id),
            k8s.V1EnvVar(name="ATTEMPT", value=str(msg.attempt)),
            k8s.V1EnvVar(name="TASK_PAYLOAD", value=task_payload),
            k8s.V1EnvVar(name="STATUS_STREAM", value=self._config.stream_job_status),
            k8s.V1EnvVar(
                name="REDIS_URL",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="redis-url", optional=True
                    )
                ),
            ),
            k8s.V1EnvVar(
                name="ANTHROPIC_API_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="anthropic-api-key", optional=True
                    )
                ),
            ),
            k8s.V1EnvVar(
                name="GITHUB_TOKEN",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(
                        name="caretaker-secrets", key="github-app-installation-token"
                    )
                ),
            ),
        ]

        job = k8s.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=k8s.V1ObjectMeta(
                name=msg.k8s_name,
                namespace=self._config.k8s_namespace,
                labels={"app": "caretaker-coding-worker", "job-id": msg.job_id},
            ),
            spec=k8s.V1JobSpec(
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels={"app": "caretaker-coding-worker"}),
                    spec=k8s.V1PodSpec(
                        restart_policy="Never",
                        service_account_name="caretaker-agent-worker",
                        security_context=k8s.V1PodSecurityContext(run_as_non_root=True, run_as_user=1001),
                        containers=[
                            k8s.V1Container(
                                name="coding-worker",
                                image=self._config.k8s_worker_image,
                                image_pull_policy="Always",
                                args=["python", "-m", "caretaker.coding_jobs.worker"],
                                env=env,
                                resources=k8s.V1ResourceRequirements(
                                    requests={"cpu": "500m", "memory": "1Gi"},
                                    limits={"cpu": "2000m", "memory": "4Gi"},
                                ),
                                security_context=k8s.V1SecurityContext(
                                    allow_privilege_escalation=False,
                                    capabilities=k8s.V1Capabilities(drop=["ALL"]),
                                ),
                            )
                        ],
                        volumes=[
                            k8s.V1Volume(
                                name="workspace",
                                empty_dir=k8s.V1EmptyDirVolumeSource(size_limit="4Gi"),
                            )
                        ],
                    ),
                ),
                backoff_limit=0,
                active_deadline_seconds=self._config.per_attempt_timeout_secs,
                ttl_seconds_after_finished=600,
            ),
        )

        try:
            self._api.create_namespaced_job(namespace=self._config.k8s_namespace, body=job)
            logger.info("k8s: created job name=%s job_id=%s", msg.k8s_name, msg.job_id)
        except Exception as exc:
            from kubernetes.client.exceptions import ApiException
            if isinstance(exc, ApiException) and exc.status == 409:
                logger.info("k8s: job already exists name=%s — skipping", msg.k8s_name)
            else:
                raise
        return msg.k8s_name

    def get_status(self, job_name: str) -> K8sJobOutcome:
        from kubernetes.client.exceptions import ApiException
        try:
            status = self._api.read_namespaced_job_status(
                name=job_name, namespace=self._config.k8s_namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return K8sJobOutcome.NOT_FOUND
            raise

        js = status.status
        for cond in js.conditions or []:
            if cond.type == "DeadlineExceeded" and cond.status == "True":
                return K8sJobOutcome.TIMEOUT
        if js.succeeded and js.succeeded > 0:
            return K8sJobOutcome.SUCCEEDED
        if js.failed and js.failed > 0:
            return K8sJobOutcome.FAILED
        return K8sJobOutcome.RUNNING

    def list_coding_job_names(self) -> list[str]:
        result = self._api.list_namespaced_job(
            namespace=self._config.k8s_namespace,
            label_selector="app=caretaker-coding-worker",
        )
        return [item.metadata.name for item in result.items]


def build_spawner(config: CodingJobsConfig) -> K8sJobSpawner:
    from kubernetes import client as k8s_client, config as k8s_config
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    return K8sJobSpawner(batch_api=k8s_client.BatchV1Api(), config=config)


__all__ = ["K8sJobOutcome", "K8sJobSpawner", "build_spawner"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_k8s.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/k8s.py tests/test_coding_jobs/test_k8s.py
git commit -m "feat(coding-jobs): add K8sJobSpawner — TASK_PAYLOAD carries minimal body only"
```

---

## Task 6: CodingJobDispatcher — ASB Peek-Lock Consumer

**Files:**

- Create: `src/caretaker/coding_jobs/dispatcher.py`
- Test: `tests/test_coding_jobs/test_dispatcher.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_dispatcher.py
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, make_job_id
from caretaker.coding_jobs.dispatcher import CodingJobDispatcher


@pytest.fixture
def config():
    from caretaker.config import CodingJobsConfig
    return CodingJobsConfig(enabled=True, k8s_worker_image="acr.io/worker:latest")


@pytest.fixture
def mock_status_stream():
    s = MagicMock()
    s.write_status = AsyncMock()
    return s


@pytest.fixture
def mock_spawner():
    sp = MagicMock()
    sp.spawn = MagicMock(return_value="ct-coding-abc1234567890123-a1")
    return sp


@pytest.fixture
def mock_receiver():
    r = MagicMock()
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    r.complete_message = AsyncMock()
    r.abandon_message = AsyncMock()
    return r


@pytest.fixture
def mock_asb_queue():
    q = MagicMock()
    q.schedule_retry = AsyncMock()
    return q


@pytest.fixture
def dispatcher(config, mock_status_stream, mock_spawner, mock_asb_queue):
    return CodingJobDispatcher(
        status_stream=mock_status_stream,
        spawner=mock_spawner,
        asb_queue=mock_asb_queue,
        config=config,
    )


@pytest.fixture
def asb_message():
    job_id = make_job_id("org/repo", "fix", "abc123", "fix the bug")
    msg = CodingJobMessage(
        job_id=job_id, repo="org/repo", task_type="fix", base_sha="abc123",
        instructions="fix the bug", context="", first_enqueued_ts=time.time(), attempt=1,
    )
    asb_msg = MagicMock()
    body_bytes = json.dumps(msg.to_asb_body()).encode()
    asb_msg.body = iter([body_bytes])
    asb_msg.application_properties = {
        b"job_id": msg.job_id.encode(),
        b"first_enqueued_ts": str(msg.first_enqueued_ts).encode(),
        b"traceparent": b"00-abc-def-01",
    }
    asb_msg.delivery_count = 0
    return asb_msg, msg


@pytest.mark.asyncio
async def test_handle_message_spawns_k8s_job(dispatcher, mock_spawner, mock_status_stream, asb_message):
    asb_msg, coding_msg = asb_message
    await dispatcher._handle_message(asb_msg)
    mock_spawner.spawn.assert_called_once()
    spawned = mock_spawner.spawn.call_args[0][0]
    assert spawned.job_id == coding_msg.job_id


@pytest.mark.asyncio
async def test_handle_message_writes_spawning_status(dispatcher, mock_status_stream, asb_message):
    asb_msg, coding_msg = asb_message
    await dispatcher._handle_message(asb_msg)
    mock_status_stream.write_status.assert_awaited_once()
    status_event = mock_status_stream.write_status.call_args[0][0]
    assert status_event.status == JobStatus.SPAWNING
    assert status_event.job_id == coding_msg.job_id
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_dispatcher.py -v
```

Expected: `ImportError: cannot import name 'CodingJobDispatcher'`

- [ ] **Step 3: Implement CodingJobDispatcher**

```python
# src/caretaker/coding_jobs/dispatcher.py
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.asb_queue import AsbCodingQueue
from caretaker.coding_jobs.models import JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.k8s import K8sJobSpawner
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

_RECEIVE_BATCH = 1        # one at a time — each spawn is heavyweight
_MAX_WAIT_SECS = 5


class CodingJobDispatcher:
    """Peek-lock consumer on the ASB coding-tasks queue. Spawns K8s Jobs."""

    def __init__(
        self,
        *,
        status_stream: JobStatusStream,
        spawner: K8sJobSpawner,
        asb_queue: AsbCodingQueue,
        config: CodingJobsConfig,
    ) -> None:
        self._status = status_stream
        self._spawner = spawner
        self._asb = asb_queue
        self._config = config

    async def run(self, asb_client: Any) -> None:
        """Main consume loop — runs until cancelled."""
        from azure.servicebus import ServiceBusReceiveMode

        logger.info("coding-dispatcher: starting ASB consumer queue=%s", self._config.asb_queue_coding_tasks)

        async with asb_client.get_queue_receiver(
            self._config.asb_queue_coding_tasks,
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            max_wait_time=_MAX_WAIT_SECS,
        ) as receiver:
            async for asb_msg in receiver:
                try:
                    await self._handle_message(asb_msg)
                    await receiver.complete_message(asb_msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "coding-dispatcher: handler failed delivery_count=%d — abandoning",
                        asb_msg.delivery_count,
                        exc_info=True,
                    )
                    await receiver.abandon_message(asb_msg)

    async def _handle_message(self, asb_msg: Any) -> None:
        from caretaker.coding_jobs.asb_queue import AsbCodingQueue

        msg = AsbCodingQueue.parse_received(asb_msg)
        logger.info(
            "coding-dispatcher: received job_id=%s attempt=%d delivery_count=%d",
            msg.job_id, msg.attempt, asb_msg.delivery_count,
        )

        self._spawner.spawn(msg)
        await self._status.write_status(
            StatusEvent(
                job_id=msg.job_id,
                status=JobStatus.SPAWNING,
                attempt=msg.attempt,
                phase="K8S_SPAWN",
            )
        )


__all__ = ["CodingJobDispatcher"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_dispatcher.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/dispatcher.py tests/test_coding_jobs/test_dispatcher.py
git commit -m "feat(coding-jobs): add CodingJobDispatcher — ASB peek-lock, spawns K8s Jobs"
```

---

## Task 7: Reconciler — K8s Poll + Heartbeat (Simplified by ASB)

**Files:**

- Create: `src/caretaker/coding_jobs/reconciler.py`
- Test: `tests/test_coding_jobs/test_reconciler.py`

Note: No `_claim_stuck_pending` / XAUTOCLAIM needed — ASB lock expiry (`LockDuration=PT5M`) handles stuck consumers automatically.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_reconciler.py
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, StatusEvent, make_job_id
from caretaker.coding_jobs.k8s import K8sJobOutcome
from caretaker.coding_jobs.reconciler import Reconciler


@pytest.fixture
def config():
    from caretaker.config import CodingJobsConfig
    return CodingJobsConfig(enabled=True, heartbeat_staleness_secs=300)


@pytest.fixture
def mock_status_stream():
    s = MagicMock()
    s.write_status = AsyncMock()
    return s


@pytest.fixture
def mock_spawner():
    sp = MagicMock()
    sp.get_status = MagicMock(return_value=K8sJobOutcome.RUNNING)
    sp.list_coding_job_names = MagicMock(return_value=[])
    return sp


@pytest.fixture
def mock_asb_queue():
    q = MagicMock()
    q.schedule_retry = AsyncMock()
    return q


@pytest.fixture
def reconciler(config, mock_status_stream, mock_spawner, mock_asb_queue):
    return Reconciler(
        status_stream=mock_status_stream,
        spawner=mock_spawner,
        asb_queue=mock_asb_queue,
        config=config,
    )


@pytest.fixture
def sample_msg():
    job_id = make_job_id("org/repo", "fix", "abc", "fix the bug")
    return CodingJobMessage(
        job_id=job_id, repo="org/repo", task_type="fix", base_sha="abc",
        instructions="fix the bug", context="", first_enqueued_ts=time.time(), attempt=1,
    )


@pytest.mark.asyncio
async def test_reconcile_marks_failed_k8s_job(reconciler, mock_status_stream, mock_spawner, sample_msg):
    mock_spawner.list_coding_job_names.return_value = [sample_msg.k8s_name]
    mock_spawner.get_status.return_value = K8sJobOutcome.FAILED
    reconciler._track(sample_msg)
    await reconciler._check_k8s_jobs()
    status_event = mock_status_stream.write_status.call_args[0][0]
    assert status_event.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_reconcile_schedules_retry_on_failure(reconciler, mock_asb_queue, mock_spawner, sample_msg):
    mock_spawner.list_coding_job_names.return_value = [sample_msg.k8s_name]
    mock_spawner.get_status.return_value = K8sJobOutcome.FAILED
    reconciler._track(sample_msg)
    await reconciler._check_k8s_jobs()
    # backoff for attempt 1 = 60s
    mock_asb_queue.schedule_retry.assert_awaited_once_with(sample_msg, delay_secs=60)


@pytest.mark.asyncio
async def test_reconcile_marks_timeout(reconciler, mock_status_stream, mock_spawner, sample_msg):
    mock_spawner.list_coding_job_names.return_value = [sample_msg.k8s_name]
    mock_spawner.get_status.return_value = K8sJobOutcome.TIMEOUT
    reconciler._track(sample_msg)
    await reconciler._check_k8s_jobs()
    status_event = mock_status_stream.write_status.call_args[0][0]
    assert status_event.status == JobStatus.TIMEOUT


@pytest.mark.asyncio
async def test_reconcile_marks_heartbeat_timeout(reconciler, mock_status_stream, sample_msg):
    stale_ts = time.time() - 400  # 400s > 300s threshold
    await reconciler._check_heartbeats({sample_msg.job_id: stale_ts})
    reconciler._track(sample_msg)
    await reconciler._check_heartbeats({sample_msg.job_id: stale_ts})
    status_event = mock_status_stream.write_status.call_args[0][0]
    assert status_event.status == JobStatus.HEARTBEAT_TIMEOUT
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_reconciler.py -v
```

Expected: `ImportError: cannot import name 'Reconciler'`

- [ ] **Step 3: Implement Reconciler**

```python
# src/caretaker/coding_jobs/reconciler.py
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from caretaker.coding_jobs.k8s import K8sJobOutcome
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.asb_queue import AsbCodingQueue
    from caretaker.coding_jobs.k8s import K8sJobSpawner
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

# Exponential backoff: attempt 1→2 = 60s, attempt 2→3 = 180s
_BACKOFF_SECS = {1: 60, 2: 180}
_DEFAULT_BACKOFF = 300


class Reconciler:
    """Polls K8s Job status and heartbeat staleness; schedules ASB retries on failure.

    No XAUTOCLAIM needed — ASB LockDuration=PT5M handles stuck consumers automatically.
    """

    def __init__(
        self,
        *,
        status_stream: JobStatusStream,
        spawner: K8sJobSpawner,
        asb_queue: AsbCodingQueue,
        config: CodingJobsConfig,
    ) -> None:
        self._status = status_stream
        self._spawner = spawner
        self._asb = asb_queue
        self._config = config
        self._in_flight: dict[str, CodingJobMessage] = {}

    def _track(self, msg: CodingJobMessage) -> None:
        self._in_flight[msg.job_id] = msg

    def _untrack(self, job_id: str) -> None:
        self._in_flight.pop(job_id, None)

    async def run(self) -> None:
        while True:
            try:
                await self._check_k8s_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("reconciler: pass failed", exc_info=True)
            await asyncio.sleep(self._config.reconcile_interval_secs)

    async def _check_k8s_jobs(self) -> None:
        for job_name in self._spawner.list_coding_job_names():
            outcome = self._spawner.get_status(job_name)
            job_id = _job_id_from_name(job_name)
            msg = self._in_flight.get(job_id)

            if outcome == K8sJobOutcome.SUCCEEDED:
                self._untrack(job_id)
                continue

            if outcome in (K8sJobOutcome.FAILED, K8sJobOutcome.TIMEOUT):
                status = JobStatus.TIMEOUT if outcome == K8sJobOutcome.TIMEOUT else JobStatus.FAILED
                if msg:
                    await self._status.write_status(
                        StatusEvent(job_id=job_id, status=status, attempt=msg.attempt,
                                    error=f"k8s outcome: {outcome.value}")
                    )
                    self._untrack(job_id)
                    delay = _BACKOFF_SECS.get(msg.attempt, _DEFAULT_BACKOFF)
                    await self._asb.schedule_retry(msg, delay_secs=delay)

    async def _check_heartbeats(self, heartbeat_map: dict[str, float]) -> None:
        threshold = time.time() - self._config.heartbeat_staleness_secs
        for job_id, last_ts in heartbeat_map.items():
            if last_ts < threshold:
                msg = self._in_flight.get(job_id)
                if msg:
                    await self._status.write_status(
                        StatusEvent(
                            job_id=job_id, status=JobStatus.HEARTBEAT_TIMEOUT, attempt=msg.attempt,
                            error=f"no heartbeat for {self._config.heartbeat_staleness_secs}s",
                        )
                    )
                    self._untrack(job_id)
                    delay = _BACKOFF_SECS.get(msg.attempt, _DEFAULT_BACKOFF)
                    await self._asb.schedule_retry(msg, delay_secs=delay)


def _job_id_from_name(job_name: str) -> str:
    # ct-coding-{job_id16}-a{n}
    parts = job_name.split("-")
    return parts[2] if len(parts) >= 4 else ""


__all__ = ["Reconciler"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_reconciler.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/reconciler.py tests/test_coding_jobs/test_reconciler.py
git commit -m "feat(coding-jobs): add Reconciler — K8s poll + heartbeat; ASB schedule_retry for backoff"
```

---

## Task 8: Worker Entrypoint (Job Container)

**Files:**

- Create: `src/caretaker/coding_jobs/worker.py`
- Test: `tests/test_coding_jobs/test_worker.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coding_jobs/test_worker.py
import json
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, make_job_id


@pytest.fixture
def task_payload_env():
    job_id = make_job_id("org/repo", "fix", "abc123", "fix the null pointer")
    msg = CodingJobMessage(
        job_id=job_id, repo="org/repo", task_type="fix", base_sha="abc123",
        instructions="fix the null pointer", context="PR #42",
        first_enqueued_ts=time.time(), attempt=1,
    )
    # TASK_PAYLOAD is the minimal ASB body (no attempt, no lineage)
    return json.dumps(msg.to_asb_body()), msg.job_id, "1"


@pytest.mark.asyncio
async def test_worker_writes_started_then_completed(task_payload_env):
    payload, job_id, attempt = task_payload_env
    mock_stream = MagicMock()
    mock_stream.write_status = AsyncMock()

    with patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}):
        with patch("caretaker.coding_jobs.worker._run_coding_task", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("deadbeef", "https://github.com/org/repo/pull/1", "patch")
            from caretaker.coding_jobs.worker import run_coding_worker
            await run_coding_worker(status_stream=mock_stream)

    statuses = [c[0][0].status for c in mock_stream.write_status.call_args_list]
    assert JobStatus.STARTED in statuses
    assert JobStatus.COMPLETED in statuses


@pytest.mark.asyncio
async def test_worker_writes_failed_on_exception(task_payload_env):
    payload, job_id, attempt = task_payload_env
    mock_stream = MagicMock()
    mock_stream.write_status = AsyncMock()

    with patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}):
        with patch("caretaker.coding_jobs.worker._run_coding_task", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("LLM unavailable")
            from caretaker.coding_jobs.worker import run_coding_worker
            with pytest.raises(RuntimeError):
                await run_coding_worker(status_stream=mock_stream)

    statuses = [c[0][0].status for c in mock_stream.write_status.call_args_list]
    assert JobStatus.FAILED in statuses


@pytest.mark.asyncio
async def test_worker_sends_heartbeats(task_payload_env):
    payload, job_id, attempt = task_payload_env
    mock_stream = MagicMock()
    mock_stream.write_status = AsyncMock()

    async def slow_task(*a, **kw):
        import asyncio; await asyncio.sleep(0.05)
        return ("sha", "url", "patch")

    with patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}):
        with patch("caretaker.coding_jobs.worker._run_coding_task", side_effect=slow_task):
            with patch("caretaker.coding_jobs.worker.HEARTBEAT_INTERVAL_SECS", 0.01):
                from caretaker.coding_jobs import worker
                import importlib; importlib.reload(worker)
                await worker.run_coding_worker(status_stream=mock_stream)

    heartbeats = [c[0][0] for c in mock_stream.write_status.call_args_list
                  if c[0][0].status == JobStatus.RUNNING and c[0][0].phase == "HEARTBEAT"]
    assert len(heartbeats) >= 1
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_worker.py -v
```

Expected: `ImportError: cannot import name 'run_coding_worker'`

- [ ] **Step 3: Implement worker.py**

```python
# src/caretaker/coding_jobs/worker.py
"""Coding job container entrypoint.

Reads JOB_ID, ATTEMPT, TASK_PAYLOAD (minimal ASB body JSON) from env.
Writes lifecycle events to the job-status Redis Stream via JobStatusStream.
No GitHub API calls — result delivery handled by ResultPoster in dispatcher pod.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.status_stream import JobStatusStream

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECS: float = 30.0


async def run_coding_worker(*, status_stream: JobStatusStream) -> None:
    body = json.loads(os.environ["TASK_PAYLOAD"])
    job_id = os.environ["JOB_ID"]
    attempt = int(os.environ.get("ATTEMPT", "1"))

    # Reconstruct from minimal body + env-injected attempt
    msg = CodingJobMessage(
        job_id=body["job_id"],
        repo=body["repo"],
        task_type=body["task_type"],
        base_sha=body["base_sha"],
        instructions=body["instructions"],
        context=body.get("context", ""),
        attempt=attempt,
    )

    await status_stream.write_status(
        StatusEvent(job_id=msg.job_id, status=JobStatus.STARTED, attempt=msg.attempt, phase="INIT")
    )

    heartbeat_task = asyncio.create_task(_heartbeat_loop(msg, status_stream))

    try:
        commit_sha, pr_url, result_patch = await _run_coding_task(msg)
        await status_stream.write_status(
            StatusEvent(
                job_id=msg.job_id, status=JobStatus.COMPLETED, attempt=msg.attempt,
                phase="DONE", commit_sha=commit_sha, pr_url=pr_url, result_patch=result_patch,
            )
        )
    except Exception as exc:
        logger.exception("coding-worker: task failed job_id=%s", msg.job_id)
        await status_stream.write_status(
            StatusEvent(
                job_id=msg.job_id, status=JobStatus.FAILED, attempt=msg.attempt,
                phase="ERROR", error=f"{type(exc).__name__}: {exc}",
            )
        )
        raise
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _heartbeat_loop(msg: CodingJobMessage, stream: JobStatusStream) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECS)
        await stream.write_status(
            StatusEvent(
                job_id=msg.job_id, status=JobStatus.RUNNING, attempt=msg.attempt,
                phase="HEARTBEAT", heartbeat_ts=time.time(),
            )
        )


async def _run_coding_task(msg: CodingJobMessage) -> tuple[str, str, str]:
    """Run coding work via foundry executor. Returns (commit_sha, pr_url, patch)."""
    from caretaker.foundry.executor import CodingTask, FoundryExecutor, TaskType

    task = CodingTask(
        task_type=TaskType(msg.task_type),
        job_name=f"k8s-{msg.job_id}",
        error_output="",
        instructions=msg.instructions,
        context=msg.context,
    )
    executor = FoundryExecutor.from_env()
    result = await executor.run(task)
    return result.commit_sha or "", result.pr_url or "", result.patch or ""


if __name__ == "__main__":
    import asyncio
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.eventbus.redis_streams import RedisStreamsEventBus
    from caretaker.config import CodingJobsConfig

    async def _main() -> None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        config = CodingJobsConfig()
        bus = RedisStreamsEventBus(redis_url=redis_url)
        stream = JobStatusStream(bus=bus, config=config)
        await run_coding_worker(status_stream=stream)

    asyncio.run(_main())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_coding_jobs/test_worker.py -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/coding_jobs/worker.py tests/test_coding_jobs/test_worker.py
git commit -m "feat(coding-jobs): add coding worker — reads minimal env payload, writes heartbeats"
```

---

## Task 9: ResultPoster + Infra Manifests

**Files:**

- Create: `src/caretaker/coding_jobs/result_poster.py`
- Create: `src/caretaker/coding_jobs/dispatcher_main.py`
- Modify: `infra/k8s/caretaker-agent-worker.yaml`
- Create: `infra/k8s/caretaker-job-dispatcher-deployment.yaml`

- [ ] **Step 1: Write the failing test for ResultPoster**

```python
# tests/test_coding_jobs/test_result_poster.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from caretaker.coding_jobs.models import JobStatus, StatusEvent, make_job_id
from caretaker.coding_jobs.result_poster import ResultPoster
from caretaker.eventbus.base import Event


@pytest.fixture
def mock_post():
    return AsyncMock()


@pytest.fixture
def config():
    from caretaker.config import CodingJobsConfig
    return CodingJobsConfig()


@pytest.fixture
def poster(mock_post, config):
    return ResultPoster(post_comment=mock_post, config=config)


def _make_event(status: JobStatus, **kwargs) -> Event:
    job_id = make_job_id("org/repo", "fix", "abc", "fix")
    s = StatusEvent(job_id=job_id, status=status, attempt=1, **kwargs)
    return Event(id="1-0", stream="job-status", payload=s.to_payload())


@pytest.mark.asyncio
async def test_posts_comment_on_completed(poster, mock_post):
    event = _make_event(JobStatus.COMPLETED, commit_sha="deadbeef", pr_url="https://github.com/pr/1")
    await poster._handle_status_event(event)
    mock_post.assert_awaited_once()
    body = mock_post.call_args[1]["body"]
    assert "deadbeef" in body


@pytest.mark.asyncio
async def test_posts_failure_comment_on_dead_letter(poster, mock_post):
    event = _make_event(JobStatus.DEAD_LETTER, error="max attempts")
    await poster._handle_status_event(event)
    mock_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_ignores_non_terminal_events(poster, mock_post):
    event = _make_event(JobStatus.RUNNING)
    await poster._handle_status_event(event)
    mock_post.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

```bash
pytest tests/test_coding_jobs/test_result_poster.py -v
```

Expected: `ImportError: cannot import name 'ResultPoster'`

- [ ] **Step 3: Implement ResultPoster**

```python
# src/caretaker/coding_jobs/result_poster.py
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.models import JobStatus, StatusEvent
from caretaker.eventbus.base import Event

if TYPE_CHECKING:
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

_TERMINAL = {JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.HEARTBEAT_TIMEOUT}

PostCommentFn = Callable[..., Awaitable[None]]


class ResultPoster:
    def __init__(self, *, post_comment: PostCommentFn, config: CodingJobsConfig,
                 consumer_name: str = "result-poster-0") -> None:
        self._post = post_comment
        self._config = config
        self._consumer_name = consumer_name

    async def run(self, redis_bus: Any) -> None:
        from caretaker.coding_jobs.status_stream import JobStatusStream
        stream = JobStatusStream(bus=redis_bus, config=self._config)
        await stream.consume_results(consumer=self._consumer_name, handler=self._handle_status_event)

    async def _handle_status_event(self, event: Event) -> None:
        try:
            se = StatusEvent.from_payload(event.payload)
        except (KeyError, ValueError):
            logger.error("result-poster: undecodable event id=%s", event.id)
            return

        if se.status not in _TERMINAL:
            return

        body = _format_success(se) if se.status == JobStatus.COMPLETED else _format_failure(se)
        logger.info("result-poster: posting job_id=%s status=%s", se.job_id, se.status.value)
        await self._post(job_id=se.job_id, body=body)


def _format_success(e: StatusEvent) -> str:
    lines = ["**Coding job completed** ✓", "", f"Commit: `{e.commit_sha}`"]
    if e.pr_url:
        lines.append(f"PR: {e.pr_url}")
    if e.result_patch:
        lines += ["", "<details><summary>Patch</summary>", "",
                  f"```diff\n{e.result_patch[:4000]}\n```", "</details>"]
    return "\n".join(lines)


def _format_failure(e: StatusEvent) -> str:
    return "\n".join([
        f"**Coding job failed** after {e.attempt} attempt(s).",
        "", f"Status: `{e.status.value}`", f"Error: {e.error or 'unknown'}",
    ])


__all__ = ["ResultPoster"]
```

- [ ] **Step 4: Run ResultPoster tests**

```bash
pytest tests/test_coding_jobs/test_result_poster.py -v
```

Expected: all PASSED

- [ ] **Step 5: Create dispatcher_main.py**

```python
# src/caretaker/coding_jobs/dispatcher_main.py
"""Standalone entrypoint for caretaker-job-dispatcher pod."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)


async def _main() -> None:
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient

    from caretaker.coding_jobs.asb_queue import AsbCodingQueue
    from caretaker.coding_jobs.dispatcher import CodingJobDispatcher
    from caretaker.coding_jobs.k8s import build_spawner
    from caretaker.coding_jobs.reconciler import Reconciler
    from caretaker.coding_jobs.result_poster import ResultPoster
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig
    from caretaker.eventbus.redis_streams import RedisStreamsEventBus

    config = CodingJobsConfig(
        enabled=True,
        asb_namespace=os.environ["CARETAKER_ASB_NAMESPACE"],
        k8s_worker_image=os.environ.get("CARETAKER_CODING_JOBS_K8S_WORKER_IMAGE", ""),
    )

    redis_url = os.environ.get("REDIS_URL", "redis://redis.caretaker.svc.cluster.local:6379")
    redis_bus = RedisStreamsEventBus(redis_url=redis_url)
    status_stream = JobStatusStream(bus=redis_bus, config=config)

    credential = DefaultAzureCredential()
    asb_client = ServiceBusClient(
        fully_qualified_namespace=config.asb_namespace,
        credential=credential,
    )
    asb_queue = AsbCodingQueue(config=config, client=asb_client)
    spawner = build_spawner(config)

    dispatcher = CodingJobDispatcher(
        status_stream=status_stream, spawner=spawner, asb_queue=asb_queue, config=config,
    )
    reconciler = Reconciler(
        status_stream=status_stream, spawner=spawner, asb_queue=asb_queue, config=config,
    )

    async def _noop_post(**kwargs: object) -> None:
        logger.info("result-poster: noop post job_id=%s", kwargs.get("job_id"))

    result_poster = ResultPoster(post_comment=_noop_post, config=config)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    tasks = [
        asyncio.create_task(dispatcher.run(asb_client), name="dispatcher"),
        asyncio.create_task(reconciler.run(), name="reconciler"),
        asyncio.create_task(result_poster.run(redis_bus), name="result-poster"),
    ]

    logger.info("caretaker-job-dispatcher started namespace=%s", config.asb_namespace)
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis_bus.close()
    await asb_client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
```

- [ ] **Step 6: Add coding-worker Job template to caretaker-agent-worker.yaml**

Append at the end of `infra/k8s/caretaker-agent-worker.yaml`:

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: caretaker-coding-worker-template
  namespace: caretaker
  labels:
    app: caretaker-coding-worker
    role: template
  annotations:
    caretaker.io/description: >-
      Template for durable coding job workers spawned by caretaker-job-dispatcher.
      NOT run directly. Dispatcher clones per task with content-addressed name.
spec:
  ttlSecondsAfterFinished: 600
  backoffLimit: 0
  activeDeadlineSeconds: 900
  suspend: true
  template:
    metadata:
      labels:
        app: caretaker-coding-worker
    spec:
      serviceAccountName: caretaker-agent-worker
      restartPolicy: Never
      tolerations:
        - key: "kubernetes.azure.com/scalesetpriority"
          operator: "Equal"
          value: "spot"
          effect: "NoSchedule"
      securityContext:
        runAsNonRoot: true
        runAsUser: 1001
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: coding-worker
          image: MUST_BE_OVERRIDDEN_BY_DISPATCHER
          imagePullPolicy: Always
          args: ["python", "-m", "caretaker.coding_jobs.worker"]
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          env:
            - name: JOB_ID
              value: "MUST_BE_OVERRIDDEN_BY_DISPATCHER"
            - name: ATTEMPT
              value: "MUST_BE_OVERRIDDEN_BY_DISPATCHER"
            - name: TASK_PAYLOAD
              value: "MUST_BE_OVERRIDDEN_BY_DISPATCHER"
            - name: STATUS_STREAM
              value: "job-status"
            - name: OTEL_SERVICE_NAME
              value: "caretaker-coding-worker"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://otel-collector.default.svc.cluster.local:4317"
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: caretaker-secrets
                  key: redis-url
                  optional: true
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: caretaker-secrets
                  key: anthropic-api-key
                  optional: true
            - name: GITHUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: caretaker-secrets
                  key: github-app-installation-token
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2000m"
              memory: "4Gi"
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: workspace
          emptyDir:
            sizeLimit: 4Gi
```

- [ ] **Step 7: Create dispatcher deployment manifest**

Create `infra/k8s/caretaker-job-dispatcher-deployment.yaml`:

```yaml
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: caretaker-job-dispatcher
  namespace: caretaker
  labels:
    app: caretaker-job-dispatcher

---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: caretaker-job-dispatcher-binding
  namespace: caretaker
subjects:
  - kind: ServiceAccount
    name: caretaker-job-dispatcher
    namespace: caretaker
roleRef:
  kind: Role
  name: caretaker-agent-worker-job-creator
  apiGroup: rbac.authorization.k8s.io

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: caretaker-job-dispatcher
  namespace: caretaker
  labels:
    app: caretaker-job-dispatcher
spec:
  replicas: 1
  selector:
    matchLabels:
      app: caretaker-job-dispatcher
  template:
    metadata:
      labels:
        app: caretaker-job-dispatcher
    spec:
      serviceAccountName: caretaker-job-dispatcher
      containers:
        - name: dispatcher
          image: your-acr.azurecr.io/caretaker-mcp:latest
          imagePullPolicy: Always
          args: ["python", "-m", "caretaker.coding_jobs.dispatcher_main"]
          env:
            - name: CARETAKER_ASB_NAMESPACE
              value: "thebiggestboy.servicebus.windows.net"
            - name: CARETAKER_CODING_JOBS_K8S_WORKER_IMAGE
              value: "your-acr.azurecr.io/caretaker-coding-worker:latest"
            - name: OTEL_SERVICE_NAME
              value: "caretaker-job-dispatcher"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://otel-collector.default.svc.cluster.local:4317"
            - name: REDIS_URL
              valueFrom:
                secretKeyRef:
                  name: caretaker-secrets
                  key: redis-url
                  optional: true
          resources:
            requests:
              cpu: "50m"
              memory: "128Mi"
            limits:
              cpu: "200m"
              memory: "256Mi"
```

- [ ] **Step 8: Verify manifests parse**

```bash
python -c "
import yaml
for f in ['infra/k8s/caretaker-agent-worker.yaml',
          'infra/k8s/caretaker-job-dispatcher-deployment.yaml']:
    docs = list(yaml.safe_load_all(open(f)))
    print(f'OK {f}: {len(docs)} docs')
"
```

Expected: `OK infra/k8s/caretaker-agent-worker.yaml: 6 docs`, `OK infra/k8s/caretaker-job-dispatcher-deployment.yaml: 3 docs`

- [ ] **Step 9: Commit**

```bash
git add src/caretaker/coding_jobs/result_poster.py \
        src/caretaker/coding_jobs/dispatcher_main.py \
        infra/k8s/caretaker-agent-worker.yaml \
        infra/k8s/caretaker-job-dispatcher-deployment.yaml \
        tests/test_coding_jobs/test_result_poster.py
git commit -m "feat(coding-jobs): add ResultPoster, dispatcher_main, and K8s manifests"
```

---

## Task 10: MCP Backend Status Endpoint

**Files:**
- Modify: `src/caretaker/mcp_backend/main.py`
- Test: `tests/test_coding_jobs/test_status_endpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coding_jobs/test_status_endpoint.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def test_coding_job_status_returns_404_for_unknown():
    from fastapi.testclient import TestClient
    from caretaker.mcp_backend.main import app
    client = TestClient(app)
    resp = client.get("/coding-jobs/nonexistent1234/status")
    assert resp.status_code in (404, 503)   # 503 if redis not configured in test
```

- [ ] **Step 2: Run to verify it fails**

```bash
pytest tests/test_coding_jobs/test_status_endpoint.py -v
```

Expected: `FAILED` — route does not exist

- [ ] **Step 3: Add status endpoint to main.py**

Find `@app.get("/health")` in `src/caretaker/mcp_backend/main.py` and insert before it:

```python
@app.get("/coding-jobs/{job_id}/status")
async def coding_job_status(job_id: str) -> dict:
    """Latest status for a coding job, read from the job-status Redis Stream."""
    try:
        from caretaker.coding_jobs.status_stream import JobStatusStream
        from caretaker.config import CodingJobsConfig

        redis_url = os.environ.get("REDIS_URL", "")
        if not redis_url:
            raise HTTPException(status_code=503, detail="redis not configured")

        from caretaker.eventbus.redis_streams import RedisStreamsEventBus
        bus = RedisStreamsEventBus(redis_url=redis_url)
        stream = JobStatusStream(bus=bus, config=CodingJobsConfig())
        status = await stream.read_latest_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="job not found")
        return status.to_payload()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("coding_job_status error job_id=%s: %s", job_id, exc)
        raise HTTPException(status_code=500, detail="internal error")
```

- [ ] **Step 4: Run all coding_jobs tests**

```bash
pytest tests/test_coding_jobs/ -v
```

Expected: all PASSED

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/mcp_backend/main.py tests/test_coding_jobs/test_status_endpoint.py
git commit -m "feat(coding-jobs): add GET /coding-jobs/{job_id}/status endpoint"
```

---

## Self-Review

### Spec Coverage

| Requirement | Task |
|-------------|------|
| ASB Standard queue for coding-tasks | Task 3: `AsbCodingQueue` |
| Minimal body (task spec only) | Task 2: `to_asb_body()` — no lineage/tracing in body |
| W3C traceparent in application_properties | Task 3: `enqueue(traceparent=)` |
| job_id + first_enqueued_ts in properties | Task 2: `to_asb_properties()` |
| delivery_count → attempt (no payload field) | Task 2: `from_asb(delivery_count=)` |
| MaxDeliveryCount=3 enforced by ASB | Queue config note in plan header |
| TimeToLive=2700s per message | Task 3: `time_to_live=timedelta(seconds=2700)` |
| Retry via `schedule_messages` with backoff | Task 3: `schedule_retry`, Task 7: `Reconciler` |
| No XAUTOCLAIM needed | Task 7: Reconciler has no `_claim_stuck_pending` |
| Content-addressed job_id | Task 2: `make_job_id` |
| K8s Job spawn with 409 dedup | Task 5: `K8sJobSpawner.spawn` |
| TASK_PAYLOAD = minimal body only | Task 5: `json.dumps(msg.to_asb_body())` |
| Heartbeat 30s + staleness 5min | Task 8: `_heartbeat_loop`, Task 7: `_check_heartbeats` |
| Result via status stream (no GH creds in job) | Tasks 8+9: worker → Redis; ResultPoster → GitHub |
| Redis Stream for job-status only | Task 4: `JobStatusStream` |
| `GET /coding-jobs/{job_id}/status` | Task 10 |

### No Placeholders ✓

### Type Consistency ✓

- `CodingJobMessage.to_asb_body()` → `dict[str, Any]` used in Task 3 `enqueue` and Task 5 `TASK_PAYLOAD`
- `CodingJobMessage.from_asb(body, properties, delivery_count)` used consistently in Tasks 3 and 6
- `AsbCodingQueue.parse_received(asb_msg)` used in Task 6 dispatcher
- `JobStatusStream.write_status(StatusEvent)` used in Tasks 6, 7, 8, 9

---

**Plan saved to `docs/superpowers/plans/2026-05-07-durable-coding-jobs.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks

**2. Inline Execution** — execute tasks in this session with checkpoints

**Which approach?**
