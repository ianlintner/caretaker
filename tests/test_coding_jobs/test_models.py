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
