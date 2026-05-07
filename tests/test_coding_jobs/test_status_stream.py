from unittest.mock import AsyncMock, MagicMock

import pytest

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
