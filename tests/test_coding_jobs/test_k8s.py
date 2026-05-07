import time
from unittest.mock import MagicMock

import pytest

from caretaker.coding_jobs.k8s import K8sJobOutcome, K8sJobSpawner
from caretaker.coding_jobs.models import CodingJobMessage, make_job_id


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
        job_id=job_id,
        repo="org/repo",
        task_type="fix",
        base_sha="abc123",
        instructions="fix the null pointer",
        context="PR #42",
        first_enqueued_ts=time.time(),
        attempt=1,
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
    s.status.active = 1
    s.status.succeeded = 0
    s.status.failed = 0
    s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.RUNNING


def test_get_status_succeeded(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0
    s.status.succeeded = 1
    s.status.failed = 0
    s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.SUCCEEDED


def test_get_status_failed(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0
    s.status.succeeded = 0
    s.status.failed = 1
    s.status.conditions = None
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.FAILED


def test_get_status_timeout_on_deadline_exceeded(spawner, mock_batch_api, sample_msg):
    s = MagicMock()
    s.status.active = 0
    s.status.succeeded = 0
    s.status.failed = 1
    cond = MagicMock()
    cond.type = "DeadlineExceeded"
    cond.status = "True"
    s.status.conditions = [cond]
    mock_batch_api.read_namespaced_job_status.return_value = s
    assert spawner.get_status(sample_msg.k8s_name) == K8sJobOutcome.TIMEOUT


def test_spawn_mounts_workspace_volume(spawner, mock_batch_api, sample_msg):
    mock_batch_api.create_namespaced_job.return_value = MagicMock()
    spawner.spawn(sample_msg)
    body = mock_batch_api.create_namespaced_job.call_args[1]["body"]
    container = body.spec.template.spec.containers[0]
    assert container.volume_mounts is not None
    mount_names = [vm.name for vm in container.volume_mounts]
    assert "workspace" in mount_names
