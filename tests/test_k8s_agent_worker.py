"""Tests for the on-demand Kubernetes agent-worker launcher + admin API."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.config import K8sAgentWorkerConfig
from caretaker.k8s_worker import api as k8s_api
from caretaker.k8s_worker.launcher import (
    K8sAgentLauncher,
    K8sLauncherError,
    build_job_manifest,
    build_job_name,
)

# ── Config defaults ───────────────────────────────────────────────────────


def test_config_defaults_disabled() -> None:
    cfg = K8sAgentWorkerConfig()
    assert cfg.enabled is False
    assert cfg.namespace == "caretaker"
    assert cfg.service_account == "caretaker-agent-worker"
    assert cfg.dedupe_ttl_seconds == 900


# ── Job name synthesis ────────────────────────────────────────────────────


def test_build_job_name_under_63_chars() -> None:
    name = build_job_name(
        name_prefix="caretaker-agent",
        repo="ianlintner/very-long-repository-name-here",
        issue_number=12345,
        task_type="LINT_FAILURE",
    )
    assert len(name) <= 63
    assert name.startswith("caretaker-agent-")


def test_build_job_name_dns_safe() -> None:
    name = build_job_name(
        name_prefix="caretaker-agent",
        repo="ian/Weird_Repo.Name",
        issue_number=1,
        task_type="REVIEW_COMMENT",
    )
    # Only lowercase letters, digits, hyphens.
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)


def test_build_job_name_deterministic_within_minute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import datetime

    fake_now = datetime(2026, 4, 21, 3, 15, 0, tzinfo=UTC)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return fake_now if tz else fake_now.replace(tzinfo=None)

    monkeypatch.setattr("caretaker.k8s_worker.launcher.datetime", _FakeDT)

    a = build_job_name(
        name_prefix="caretaker-agent",
        repo="a/b",
        issue_number=1,
        task_type="LINT_FAILURE",
    )
    b = build_job_name(
        name_prefix="caretaker-agent",
        repo="a/b",
        issue_number=1,
        task_type="LINT_FAILURE",
    )
    assert a == b


# ── Manifest synthesis ────────────────────────────────────────────────────


def test_build_job_manifest_shape() -> None:
    cfg = K8sAgentWorkerConfig(
        enabled=True,
        namespace="caretaker",
        image="acr.example/caretaker-agent:1.2.3",
    )
    m = build_job_manifest(config=cfg, repo="ian/demo", issue_number=42, task_type="LINT_FAILURE")
    assert m["apiVersion"] == "batch/v1"
    assert m["kind"] == "Job"
    assert m["metadata"]["namespace"] == "caretaker"
    assert m["spec"]["backoffLimit"] == 0
    pod = m["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "caretaker-agent-worker"
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["image"] == "acr.example/caretaker-agent:1.2.3"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GITHUB_REPOSITORY"] == "ian/demo"
    assert env["CARETAKER_TARGET_NUMBER"] == "42"
    assert env["CARETAKER_TASK_TYPE"] == "LINT_FAILURE"


def test_build_job_manifest_extra_env_merges() -> None:
    cfg = K8sAgentWorkerConfig(enabled=True, image="img")
    m = build_job_manifest(
        config=cfg,
        repo="a/b",
        issue_number=1,
        task_type="LINT_FAILURE",
        extra_env={"ANTHROPIC_API_KEY": "xxx"},
    )
    env = {e["name"]: e["value"] for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["ANTHROPIC_API_KEY"] == "xxx"
    assert env["GITHUB_REPOSITORY"] == "a/b"


def test_build_job_manifest_labels_include_slugs() -> None:
    cfg = K8sAgentWorkerConfig(enabled=True, image="img")
    m = build_job_manifest(
        config=cfg,
        repo="ian/Weird.Repo",
        issue_number=7,
        task_type="REVIEW_COMMENT",
    )
    labels = m["metadata"]["labels"]
    assert labels["app"] == "caretaker-agent-worker"
    assert labels["caretaker.io/issue"] == "7"
    assert labels["caretaker.io/repo"]  # slugified, non-empty
    assert labels["caretaker.io/task-type"]


# ── Launcher behaviour ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_refuses_when_disabled() -> None:
    launcher = K8sAgentLauncher(config=K8sAgentWorkerConfig(enabled=False))
    with pytest.raises(K8sLauncherError):
        await launcher.dispatch(repo="a/b", issue_number=1, task_type="LINT_FAILURE")


@pytest.mark.asyncio
async def test_dispatch_validates_repo_shape() -> None:
    launcher = K8sAgentLauncher(config=K8sAgentWorkerConfig(enabled=True, image="img"))
    with pytest.raises(K8sLauncherError):
        await launcher.dispatch(repo="invalid", issue_number=1, task_type="LINT_FAILURE")


@pytest.mark.asyncio
async def test_dispatch_validates_issue_number() -> None:
    launcher = K8sAgentLauncher(config=K8sAgentWorkerConfig(enabled=True, image="img"))
    with pytest.raises(K8sLauncherError):
        await launcher.dispatch(repo="a/b", issue_number=0, task_type="LINT_FAILURE")


@pytest.mark.asyncio
async def test_dispatch_deduplicates_via_redis() -> None:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"caretaker-agent-existing-abc12345")
    redis.set = AsyncMock()
    launcher = K8sAgentLauncher(
        config=K8sAgentWorkerConfig(enabled=True, image="img", dedupe_ttl_seconds=300),
        redis=redis,
    )
    record = await launcher.dispatch(repo="a/b", issue_number=5, task_type="LINT_FAILURE")
    assert record.deduped is True
    assert record.job_name == "caretaker-agent-existing-abc12345"
    redis.set.assert_not_called()  # never writes when a hit is returned


@pytest.mark.asyncio
async def test_dispatch_creates_job_and_stores_dedupe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()

    # Stub the batch API so we don't talk to a real cluster.
    batch_api = MagicMock()
    batch_api.create_namespaced_job = MagicMock(return_value=None)

    launcher = K8sAgentLauncher(
        config=K8sAgentWorkerConfig(enabled=True, image="img"),
        redis=redis,
    )
    launcher._batch_api = batch_api  # type: ignore[attr-defined]  (test hook)

    record = await launcher.dispatch(repo="a/b", issue_number=9, task_type="LINT_FAILURE")
    assert record.deduped is False
    assert record.job_name.startswith("caretaker-agent-")
    batch_api.create_namespaced_job.assert_called_once()
    kwargs = batch_api.create_namespaced_job.call_args.kwargs
    assert kwargs["namespace"] == "caretaker"
    body = kwargs["body"]
    assert body["kind"] == "Job"
    redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_propagates_api_failure() -> None:
    batch_api = MagicMock()
    batch_api.create_namespaced_job = MagicMock(side_effect=RuntimeError("boom"))
    launcher = K8sAgentLauncher(config=K8sAgentWorkerConfig(enabled=True, image="img"))
    launcher._batch_api = batch_api  # type: ignore[attr-defined]
    with pytest.raises(K8sLauncherError):
        await launcher.dispatch(repo="a/b", issue_number=1, task_type="LINT_FAILURE")


# ── Admin endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def admin_client(monkeypatch):  # type: ignore[no-untyped-def]
    from caretaker.admin import auth as admin_auth

    # Fresh module state so tests don't leak launcher across runs.
    k8s_api._launcher = None  # type: ignore[attr-defined]
    k8s_api._config = None  # type: ignore[attr-defined]

    launcher = MagicMock(spec=K8sAgentLauncher)
    from datetime import datetime

    from caretaker.k8s_worker.launcher import DispatchRecord

    async def _dispatch(*, repo, issue_number, task_type, image=None, extra_env=None):
        return DispatchRecord(
            job_name=f"caretaker-agent-{repo.split('/')[-1]}-{issue_number}",
            namespace="caretaker",
            repo=repo,
            issue_number=issue_number,
            task_type=task_type,
            created_at=datetime.now(UTC),
            deduped=False,
        )

    launcher.dispatch = AsyncMock(side_effect=_dispatch)
    launcher.list_recent = AsyncMock(return_value=[{"name": "x"}])

    cfg = K8sAgentWorkerConfig(enabled=True, image="img")
    k8s_api.configure(launcher, cfg)

    app = FastAPI()
    app.include_router(k8s_api.router)

    async def _fake_user():  # noqa: ANN202
        return admin_auth.UserInfo(sub="t", email="t@example.com", name="T", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app), launcher


def test_admin_post_creates_task(admin_client) -> None:  # type: ignore[no-untyped-def]
    client, launcher = admin_client
    r = client.post(
        "/api/admin/agent-tasks",
        json={"repo": "ian/demo", "issue_number": 42, "task_type": "LINT_FAILURE"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["repo"] == "ian/demo"
    assert data["issue_number"] == 42
    assert data["job_name"].startswith("caretaker-agent-")
    launcher.dispatch.assert_awaited_once()


def test_admin_post_returns_400_on_launcher_error(admin_client) -> None:  # type: ignore[no-untyped-def]
    client, launcher = admin_client
    launcher.dispatch = AsyncMock(side_effect=K8sLauncherError("bad repo"))
    r = client.post(
        "/api/admin/agent-tasks",
        json={"repo": "ian/demo", "issue_number": 1, "task_type": "LINT_FAILURE"},
    )
    assert r.status_code == 400
    assert "bad repo" in r.json()["detail"]


def test_admin_get_lists_jobs(admin_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = admin_client
    r = client.get("/api/admin/agent-tasks")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_admin_post_503_when_unconfigured() -> None:
    k8s_api._launcher = None  # type: ignore[attr-defined]
    k8s_api._config = None  # type: ignore[attr-defined]

    from caretaker.admin import auth as admin_auth

    app = FastAPI()
    app.include_router(k8s_api.router)

    async def _fake_user():  # noqa: ANN202
        return admin_auth.UserInfo(sub="t", email="t", name="t", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    client = TestClient(app)
    r = client.post(
        "/api/admin/agent-tasks",
        json={"repo": "a/b", "issue_number": 1, "task_type": "LINT_FAILURE"},
    )
    assert r.status_code == 503
