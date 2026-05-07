from caretaker.coding_jobs.models import (
    CodingJobMessage,
    JobStatus,
    StatusEvent,
    k8s_job_name,
    make_job_id,
)
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
    assert cfg.asb_lock_duration_secs == 300
    assert cfg.status_consumer_group == "coding-results"
    assert cfg.k8s_worker_image == ""


def test_coding_jobs_config_from_dict():
    cfg = CodingJobsConfig(
        **{"enabled": True, "asb_namespace": "thebiggestboy.servicebus.windows.net"}
    )
    assert cfg.enabled is True
    assert cfg.asb_namespace == "thebiggestboy.servicebus.windows.net"


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
    assert recovered.attempt == 2  # delivery_count=1 → attempt=2
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
