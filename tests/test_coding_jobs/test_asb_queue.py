import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.coding_jobs.asb_queue import AsbCodingQueue
from caretaker.coding_jobs.models import CodingJobMessage, make_job_id


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
    assert sent.time_to_live == timedelta(seconds=2700)


@pytest.mark.asyncio
async def test_schedule_retry_uses_scheduled_enqueue_time(
    config, mock_client, mock_sender, sample_msg
):
    queue = AsbCodingQueue(config=config, client=mock_client)
    before = datetime.now(UTC)
    await queue.schedule_retry(sample_msg, delay_secs=60)
    after = datetime.now(UTC)

    mock_sender.schedule_messages.assert_awaited_once()
    call_args = mock_sender.schedule_messages.call_args
    scheduled_time = call_args[0][1]
    assert before + timedelta(seconds=55) <= scheduled_time <= after + timedelta(seconds=65)
    # Body still minimal
    msg = call_args[0][0]
    body = json.loads(b"".join(msg.body))
    assert "attempt" not in body


@pytest.mark.asyncio
async def test_schedule_retry_drops_when_ttl_exhausted(
    config, mock_client, mock_sender, sample_msg
):
    """If wall-clock TTL has elapsed, schedule_retry silently drops the message."""
    # Simulate first_enqueued_ts 46 minutes ago (beyond 45-min / 2700-sec TTL)
    expired_msg = dataclasses.replace(sample_msg, first_enqueued_ts=time.time() - 2760)
    queue = AsbCodingQueue(config=config, client=mock_client)
    await queue.schedule_retry(expired_msg, delay_secs=60)

    # schedule_messages must NOT have been called
    mock_sender.schedule_messages.assert_not_awaited()


def _make_msg():
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


def test_parse_received_happy_path():
    msg = _make_msg()
    body_bytes = json.dumps(msg.to_asb_body()).encode()
    props = msg.to_asb_properties(traceparent="00-abc-def-01")
    # ASB SDK returns bytes keys (and bytes values for strings)
    bytes_props = {k.encode(): (v.encode() if isinstance(v, str) else v) for k, v in props.items()}

    mock_asb = MagicMock()
    mock_asb.body = iter([body_bytes])
    mock_asb.application_properties = bytes_props
    mock_asb.delivery_count = 0

    result = AsbCodingQueue.parse_received(mock_asb)
    assert result.job_id == msg.job_id
    assert result.attempt == 1  # delivery_count=0 → attempt=1


def test_parse_received_delivery_count_maps_to_attempt():
    msg = _make_msg()
    body_bytes = json.dumps(msg.to_asb_body()).encode()
    props = {k.encode(): v.encode() for k, v in msg.to_asb_properties().items()}

    mock_asb = MagicMock()
    mock_asb.body = iter([body_bytes])
    mock_asb.application_properties = props
    mock_asb.delivery_count = 2

    result = AsbCodingQueue.parse_received(mock_asb)
    assert result.attempt == 3  # delivery_count=2 → attempt=3


def test_parse_received_missing_context_defaults_to_empty():
    msg = _make_msg()
    body = msg.to_asb_body()
    body.pop("context", None)  # ensure context absent
    body_bytes = json.dumps(body).encode()
    props = {k.encode(): v.encode() for k, v in msg.to_asb_properties().items()}

    mock_asb = MagicMock()
    mock_asb.body = iter([body_bytes])
    mock_asb.application_properties = props
    mock_asb.delivery_count = 0

    result = AsbCodingQueue.parse_received(mock_asb)
    assert result.context == ""
