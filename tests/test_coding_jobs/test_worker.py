import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, make_job_id


@pytest.fixture
def task_payload_env():
    job_id = make_job_id("org/repo", "fix", "abc123", "fix the null pointer")
    msg = CodingJobMessage(
        job_id=job_id,
        repo="org/repo",
        task_type="fix",
        base_sha="abc123",
        instructions="fix the null pointer",
        context="PR #42",
        first_enqueued_ts=time.time(),
        attempt=1,
    )
    # TASK_PAYLOAD is the minimal ASB body (no attempt, no lineage)
    return json.dumps(msg.to_asb_body()), msg.job_id, "1"


@pytest.mark.asyncio
async def test_worker_writes_started_then_completed(task_payload_env):
    payload, job_id, attempt = task_payload_env
    mock_stream = MagicMock()
    mock_stream.write_status = AsyncMock()

    with (
        patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}),
        patch("caretaker.coding_jobs.worker._run_coding_task", new_callable=AsyncMock) as mock_run,
    ):
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

    with (
        patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}),
        patch("caretaker.coding_jobs.worker._run_coding_task", new_callable=AsyncMock) as mock_run,
    ):
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
        import asyncio

        await asyncio.sleep(0.05)
        return ("sha", "url", "patch")

    with (
        patch.dict(os.environ, {"TASK_PAYLOAD": payload, "JOB_ID": job_id, "ATTEMPT": attempt}),
        patch("caretaker.coding_jobs.worker._run_coding_task", side_effect=slow_task),
        patch("caretaker.coding_jobs.worker.HEARTBEAT_INTERVAL_SECS", 0.01),
    ):
        from caretaker.coding_jobs.worker import run_coding_worker

        await run_coding_worker(status_stream=mock_stream)

    heartbeats = [
        c[0][0]
        for c in mock_stream.write_status.call_args_list
        if c[0][0].status == JobStatus.RUNNING and c[0][0].phase == "HEARTBEAT"
    ]
    assert len(heartbeats) >= 1
