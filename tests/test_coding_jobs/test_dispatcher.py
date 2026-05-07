import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.coding_jobs.dispatcher import CodingJobDispatcher
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, make_job_id


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
        job_id=job_id,
        repo="org/repo",
        task_type="fix",
        base_sha="abc123",
        instructions="fix the bug",
        context="",
        first_enqueued_ts=time.time(),
        attempt=1,
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
async def test_handle_message_spawns_k8s_job(
    dispatcher, mock_spawner, mock_status_stream, asb_message
):
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
