from unittest.mock import AsyncMock

import pytest

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
    event = _make_event(
        JobStatus.COMPLETED, commit_sha="deadbeef", pr_url="https://github.com/pr/1"
    )
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
