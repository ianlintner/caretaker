"""Tests for the opt-in fleet-registry feature.

Covers:
* Config defaults are safe (feature disabled unless explicitly enabled).
* Emitter fails open on unreachable endpoints / network errors.
* OAuth2 client_credentials bearer-token auth on POST /api/fleet/heartbeat.
* ``POST /api/fleet/heartbeat`` records clients into the singleton
  store; ``GET /api/admin/fleet`` (when configured) reads them back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.config import FleetRegistryConfig, MaintainerConfig, OAuth2ClientConfig
from caretaker.fleet import (
    FleetHeartbeat,
    FleetOAuthClientCache,
    build_heartbeat,
    emit_heartbeat,
    get_store,
    public_router,
    reset_store_for_tests,
)
from caretaker.state.models import RunSummary

# ── Config defaults ───────────────────────────────────────────────────────


def test_fleet_config_defaults_disabled() -> None:
    cfg = MaintainerConfig()
    assert cfg.fleet_registry.enabled is False
    assert cfg.fleet_registry.endpoint is None
    assert cfg.fleet_registry.secret_env == "CARETAKER_FLEET_SECRET"
    assert cfg.fleet_registry.include_full_summary is False


def test_fleet_config_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    yaml_path = tmp_path / "config.yml"
    yaml_path.write_text(
        "version: v1\n"
        "fleet_registry:\n"
        "  enabled: true\n"
        "  endpoint: https://example.invalid/api/fleet/heartbeat\n"
    )
    cfg = MaintainerConfig.from_yaml(yaml_path)
    assert cfg.fleet_registry.enabled is True
    assert cfg.fleet_registry.endpoint == "https://example.invalid/api/fleet/heartbeat"


# ── Heartbeat builder ─────────────────────────────────────────────────────


def _summary_fixture() -> RunSummary:
    return RunSummary(
        run_at=datetime(2026, 4, 20, 23, 0, 0, tzinfo=UTC),
        mode="full",
        prs_monitored=3,
        prs_merged=1,
        issues_triaged=2,
        goal_health=0.82,
        errors=["boom"],
    )


def test_build_heartbeat_shape(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    cfg = MaintainerConfig(fleet_registry=FleetRegistryConfig(enabled=True))
    hb = build_heartbeat(cfg, _summary_fixture())
    assert isinstance(hb, FleetHeartbeat)
    assert hb.repo == "ianlintner/demo"
    assert hb.mode == "full"
    assert hb.counters["prs_monitored"] == 3
    assert hb.counters["prs_merged"] == 1
    assert hb.counters["issues_triaged"] == 2
    assert hb.goal_health == pytest.approx(0.82)
    assert hb.error_count == 1
    assert hb.summary is None  # include_full_summary default False


def test_build_heartbeat_full_summary_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    cfg = MaintainerConfig(
        fleet_registry=FleetRegistryConfig(enabled=True, include_full_summary=True)
    )
    hb = build_heartbeat(cfg, _summary_fixture())
    assert hb.summary is not None
    assert hb.summary["prs_monitored"] == 3


# ── Emitter fail-open behaviour ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_emitter_returns_false_when_disabled() -> None:
    cfg = MaintainerConfig()  # default disabled
    assert await emit_heartbeat(cfg, _summary_fixture()) is False


@pytest.mark.asyncio
async def test_emitter_returns_false_when_endpoint_empty() -> None:
    cfg = MaintainerConfig(fleet_registry=FleetRegistryConfig(enabled=True))
    assert await emit_heartbeat(cfg, _summary_fixture()) is False


@pytest.mark.asyncio
async def test_emitter_fails_open_on_transport_error(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    cfg = MaintainerConfig(
        fleet_registry=FleetRegistryConfig(
            enabled=True,
            endpoint="http://127.0.0.1:1/doesnotexist",  # reserved port
            timeout_seconds=0.5,
        )
    )
    # Must return False, never raise.
    result = await emit_heartbeat(cfg, _summary_fixture())
    assert result is False


@pytest.mark.asyncio
async def test_emitter_attaches_oauth_bearer_and_caches_token(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    monkeypatch.setenv("OAUTH2_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "csec")
    monkeypatch.setenv("OAUTH2_TOKEN_URL", "https://auth.test/oauth/token")
    # Per-test OAuth cache — avoids any cross-test leak from a module global.
    oauth_cache = FleetOAuthClientCache()

    cfg = MaintainerConfig(
        fleet_registry=FleetRegistryConfig(
            enabled=True,
            endpoint="https://fleet.example/heartbeat",
            oauth2=OAuth2ClientConfig(enabled=True),
        )
    )

    token_calls = 0
    heartbeat_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if str(request.url) == "https://auth.test/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "bearer-abc", "expires_in": 3600},
            )
        heartbeat_calls.append(request.headers.get("authorization", ""))
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (
            await emit_heartbeat(cfg, _summary_fixture(), client=client, oauth_cache=oauth_cache)
            is True
        )
        # Second heartbeat in the same process reuses the cached token.
        assert (
            await emit_heartbeat(cfg, _summary_fixture(), client=client, oauth_cache=oauth_cache)
            is True
        )

    assert token_calls == 1  # JWT cache prevents a second token fetch
    assert heartbeat_calls == ["Bearer bearer-abc", "Bearer bearer-abc"]


@pytest.mark.asyncio
async def test_emitter_oauth_token_failure_is_swallowed(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    monkeypatch.setenv("OAUTH2_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "csec")
    monkeypatch.setenv("OAUTH2_TOKEN_URL", "https://auth.test/oauth/token")
    oauth_cache = FleetOAuthClientCache()

    cfg = MaintainerConfig(
        fleet_registry=FleetRegistryConfig(
            enabled=True,
            endpoint="https://fleet.example/heartbeat",
            oauth2=OAuth2ClientConfig(enabled=True),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://auth.test/oauth/token":
            return httpx.Response(500, text="auth server down")
        # Heartbeat should still go through, just without a bearer header.
        assert "authorization" not in {k.lower() for k in request.headers}
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (
            await emit_heartbeat(cfg, _summary_fixture(), client=client, oauth_cache=oauth_cache)
            is True
        )


@pytest.mark.asyncio
async def test_emitter_oauth_caches_are_per_instance(monkeypatch) -> None:
    """Two caches in the same process must not share a client.

    Regression test for the pre-0.12.1 module-global `_oauth_client` that
    let a second MaintainerConfig's creds silently win over the first.
    """
    monkeypatch.setenv("GITHUB_REPOSITORY", "ianlintner/demo")
    monkeypatch.setenv("OAUTH2_CLIENT_ID", "cid-A")
    monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "csec-A")
    monkeypatch.setenv("OAUTH2_TOKEN_URL", "https://auth.test/oauth/token")

    cache_a = FleetOAuthClientCache()
    cache_b = FleetOAuthClientCache()
    cfg = MaintainerConfig(
        fleet_registry=FleetRegistryConfig(
            enabled=True,
            endpoint="https://fleet.example/heartbeat",
            oauth2=OAuth2ClientConfig(enabled=True),
        )
    )

    seen_basic_auths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://auth.test/oauth/token":
            seen_basic_auths.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (
            await emit_heartbeat(cfg, _summary_fixture(), client=client, oauth_cache=cache_a)
            is True
        )
        # Rotate the secret mid-process — a real deploy during credential
        # rotation. cache_b must see the new creds; it does not share state
        # with cache_a.
        monkeypatch.setenv("OAUTH2_CLIENT_ID", "cid-B")
        monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "csec-B")
        assert (
            await emit_heartbeat(cfg, _summary_fixture(), client=client, oauth_cache=cache_b)
            is True
        )

    assert len(seen_basic_auths) == 2
    assert seen_basic_auths[0] != seen_basic_auths[1]


# ── Backend endpoints ─────────────────────────────────────────────────────


@pytest.fixture
def fleet_client(monkeypatch):  # type: ignore[no-untyped-def]
    """FastAPI client that mounts the public + admin routers without
    the full MCP lifespan (which pulls in OIDC config). Admin endpoints
    are exercised with the dependency override shortcut."""
    from caretaker.admin import auth as admin_auth
    from caretaker.auth.bearer import BearerPrincipal
    from caretaker.fleet import admin_router
    from caretaker.fleet import api as fleet_api

    reset_store_for_tests()
    monkeypatch.delenv("CARETAKER_FLEET_SECRET", raising=False)

    app = FastAPI()
    app.include_router(public_router)
    app.include_router(admin_router)

    # Bypass OIDC — the admin endpoints call ``require_session`` which
    # in turn depends on a session cookie. Override it so the test can
    # exercise the admin surface without spinning up OIDC.
    async def _fake_user():  # noqa: ANN202
        return admin_auth.UserInfo(sub="test", email="test@example.com", name="Test", picture=None)

    # Bypass OAuth2 bearer-token verification on the public heartbeat
    # endpoint. ``_REQUIRE_FLEET_TOKEN`` is the Depends() instance; the
    # actual callable FastAPI registers is ``.dependency``.
    async def _fake_principal():  # noqa: ANN202
        return BearerPrincipal(
            client_id="test-client",
            scopes=frozenset({"fleet:heartbeat"}),
            raw_claims={"client_id": "test-client", "scope": "fleet:heartbeat"},
        )

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    app.dependency_overrides[fleet_api._REQUIRE_FLEET_TOKEN.dependency] = _fake_principal
    return TestClient(app)


def test_heartbeat_roundtrip_no_secret(fleet_client) -> None:  # type: ignore[no-untyped-def]
    body = {
        "schema_version": 1,
        "repo": "ianlintner/demo",
        "caretaker_version": "0.11.0",
        "run_at": datetime.now(UTC).isoformat(),
        "mode": "full",
        "enabled_agents": ["pr_agent", "issue_agent"],
        "goal_health": 0.9,
        "error_count": 0,
        "counters": {"prs_monitored": 2},
    }
    resp = fleet_client.post("/api/fleet/heartbeat", json=body)
    assert resp.status_code == 200
    assert resp.json()["repo"] == "ianlintner/demo"

    listed = fleet_client.get("/api/admin/fleet")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] == 1
    assert payload["items"][0]["repo"] == "ianlintner/demo"
    assert payload["items"][0]["last_counters"]["prs_monitored"] == 2

    summary = fleet_client.get("/api/admin/fleet/summary")
    assert summary.status_code == 200
    s = summary.json()
    assert s["total_clients"] == 1
    assert s["version_distribution"] == {"0.11.0": 1}


def test_heartbeat_rejects_empty_body(fleet_client) -> None:  # type: ignore[no-untyped-def]
    resp = fleet_client.post(
        "/api/fleet/heartbeat",
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_heartbeat_rejects_missing_repo(fleet_client) -> None:  # type: ignore[no-untyped-def]
    resp = fleet_client.post(
        "/api/fleet/heartbeat",
        json={"caretaker_version": "0.11.0"},
    )
    assert resp.status_code == 400


def test_admin_single_repo_fetch(fleet_client) -> None:  # type: ignore[no-untyped-def]
    fleet_client.post(
        "/api/fleet/heartbeat",
        json={"repo": "ianlintner/demo", "caretaker_version": "0.11.0"},
    )
    resp = fleet_client.get("/api/admin/fleet/ianlintner/demo")
    assert resp.status_code == 200
    assert resp.json()["repo"] == "ianlintner/demo"

    missing = fleet_client.get("/api/admin/fleet/other/missing")
    assert missing.status_code == 404


def test_store_singleton_survives_test_reset() -> None:
    store = get_store()
    reset_store_for_tests()
    assert get_store() is not store
