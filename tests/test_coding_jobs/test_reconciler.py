import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.coding_jobs.k8s import K8sJobOutcome
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, make_job_id
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
        job_id=job_id,
        repo="org/repo",
        task_type="fix",
        base_sha="abc",
        instructions="fix the bug",
        context="",
        first_enqueued_ts=time.time(),
        attempt=1,
    )


@pytest.mark.asyncio
async def test_reconcile_marks_failed_k8s_job(
    reconciler, mock_status_stream, mock_spawner, sample_msg
):
    mock_spawner.list_coding_job_names.return_value = [sample_msg.k8s_name]
    mock_spawner.get_status.return_value = K8sJobOutcome.FAILED
    reconciler._track(sample_msg)
    await reconciler._check_k8s_jobs()
    status_event = mock_status_stream.write_status.call_args[0][0]
    assert status_event.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_reconcile_schedules_retry_on_failure(
    reconciler, mock_asb_queue, mock_spawner, sample_msg
):
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
