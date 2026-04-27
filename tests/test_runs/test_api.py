"""End-to-end FastAPI tests for /runs/* with mocked OIDC."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.auth import bearer, github_oidc
from caretaker.runs import api as runs_api
from caretaker.runs.store import RunsStore, set_store


@pytest.fixture(autouse=True)
def _reset_global_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CARETAKER_RUNS_INGEST_TOKEN_SECRET",
        "test-ingest-secret-XXXXXXXXXXXXXXXXXXXXXXXX",
    )
    bearer.reset()
    github_oidc.reset()
    runs_api.reset()
    set_store(RunsStore())  # in-memory
    yield
    bearer.reset()
    github_oidc.reset()
    runs_api.reset()
    set_store(None)


@pytest.fixture
def fake_principal() -> github_oidc.ActionsPrincipal:
    return github_oidc.ActionsPrincipal(
        repository="ianlintner/caretaker",
        repository_id=42,
        repository_owner="ianlintner",
        repository_owner_id=4242,
        run_id=999,
        run_attempt=1,
        actor="ianlintner",
        event_name="schedule",
        ref="refs/heads/main",
        sha="deadbeef",
        workflow="Caretaker Maintainer",
        job_workflow_ref="ianlintner/caretaker/.github/workflows/maintainer.yml@refs/heads/main",
        sub="repo:ianlintner/caretaker:ref:refs/heads/main",
        raw_claims={},
    )


@pytest.fixture
def app(fake_principal: github_oidc.ActionsPrincipal) -> FastAPI:
    application = FastAPI()
    application.include_router(runs_api.router)

    # Override the OIDC dependency factory so tests bypass JWT verification.
    async def _fake_dep() -> github_oidc.ActionsPrincipal:
        return fake_principal

    application.dependency_overrides[github_oidc.require_actions_principal()] = _fake_dep
    # The dependency factory returns a fresh function each call — override
    # by introspecting the route dependencies at request time.
    return application


def _override_principal(client: TestClient, principal: github_oidc.ActionsPrincipal) -> TestClient:
    """Replace the principal dependency on the app the client wraps."""

    async def _fake_dep() -> github_oidc.ActionsPrincipal:
        return principal

    # require_actions_principal returns a NEW callable per call; the cleanest
    # override is to monkey-patch every Depends factory in the runs router.
    client.app.dependency_overrides[next(iter(_collect_oidc_deps(client.app)))] = _fake_dep
    return client


def _collect_oidc_deps(app: FastAPI) -> set[Any]:
    """Find all OIDC-dependency callables registered on the app's routes."""
    out: set[Any] = set()
    for route in app.router.routes:
        for dep in (
            getattr(route, "dependant", None).dependencies if hasattr(route, "dependant") else []
        ):
            if dep.call is not None and dep.call.__qualname__.startswith(
                "require_actions_principal"
            ):
                out.add(dep.call)
    return out


@pytest.mark.asyncio
async def test_start_run_idempotent(
    app: FastAPI, fake_principal: github_oidc.ActionsPrincipal
) -> None:
    # Override every register_actions_principal Depends with our fake.
    deps = _collect_oidc_deps(app)
    assert deps, "expected at least one require_actions_principal dependency"

    async def _fake_dep() -> github_oidc.ActionsPrincipal:
        return fake_principal

    for d in deps:
        app.dependency_overrides[d] = _fake_dep

    client = TestClient(app)
    payload = {"mode": "full", "config_digest": "abc"}
    r1 = client.post("/runs/start", json=payload)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    run_id = body1["run_id"]
    assert body1["ingest_token"]
    assert body1["log_endpoint"].endswith(f"/runs/{run_id}/logs")
    assert body1["stream_url"].endswith(f"/runs/{run_id}/stream")

    r2 = client.post("/runs/start", json=payload)
    assert r2.status_code == 200
    assert r2.json()["run_id"] == run_id


@pytest.mark.asyncio
async def test_logs_endpoint_dedupes(
    app: FastAPI, fake_principal: github_oidc.ActionsPrincipal
) -> None:
    deps = _collect_oidc_deps(app)

    async def _fake_dep() -> github_oidc.ActionsPrincipal:
        return fake_principal

    for d in deps:
        app.dependency_overrides[d] = _fake_dep

    client = TestClient(app)
    start = client.post("/runs/start", json={"mode": "full"}).json()
    run_id = start["run_id"]
    token = start["ingest_token"]

    ndjson = "\n".join(
        [
            json.dumps({"seq": 1, "stream": "stdout", "data": "hello"}),
            json.dumps({"seq": 2, "stream": "stdout", "data": "world"}),
            json.dumps({"seq": 2, "stream": "stdout", "data": "dup"}),
        ]
    )
    r = client.post(
        f"/runs/{run_id}/logs",
        content=ndjson,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-ndjson",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 2
    assert body["duplicate"] == 1
    assert body["malformed"] == 0


@pytest.mark.asyncio
async def test_finish_terminal_idempotency(
    app: FastAPI, fake_principal: github_oidc.ActionsPrincipal
) -> None:
    deps = _collect_oidc_deps(app)

    async def _fake_dep() -> github_oidc.ActionsPrincipal:
        return fake_principal

    for d in deps:
        app.dependency_overrides[d] = _fake_dep

    client = TestClient(app)
    start = client.post("/runs/start", json={"mode": "full"}).json()
    run_id = start["run_id"]
    token = start["ingest_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r1 = client.post(f"/runs/{run_id}/finish", json={"exit_code": 0}, headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "succeeded"

    # Re-finishing returns the same status without flipping
    r2 = client.post(f"/runs/{run_id}/finish", json={"exit_code": 1}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "succeeded"
