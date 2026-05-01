"""Tests for the admin cooldown endpoints."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import api as admin_api
from caretaker.admin import auth as admin_auth
from caretaker.github_client.rate_limit import get_cooldown, reset_for_tests


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_api.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="u", email="u@example.com", name="U", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    reset_for_tests()


class TestCooldownGet:
    def test_returns_snapshot_when_clear(self, client: TestClient) -> None:
        response = client.get("/api/admin/cooldown")
        assert response.status_code == 200
        body = response.json()
        assert body["blocked"] is False
        assert body["seconds_remaining"] == 0
        assert body["last_remaining"] is None

    def test_returns_blocked_state(self, client: TestClient) -> None:
        cd = get_cooldown()
        cd.mark_blocked(time.time() + 600.0, reason="test")

        response = client.get("/api/admin/cooldown")
        assert response.status_code == 200
        body = response.json()
        assert body["blocked"] is True
        assert body["seconds_remaining"] > 0
        assert body["reason"] == "test"


class TestCooldownReset:
    def test_clears_active_cooldown(self, client: TestClient) -> None:
        cd = get_cooldown()
        cd.mark_blocked(time.time() + 600.0, reason="stuck")
        assert cd.is_blocked()

        response = client.post("/api/admin/cooldown/reset")
        assert response.status_code == 200
        body = response.json()
        assert body["blocked"] is False
        assert body["seconds_remaining"] == 0
        assert cd.is_blocked() is False

    def test_no_op_when_already_clear(self, client: TestClient) -> None:
        response = client.post("/api/admin/cooldown/reset")
        assert response.status_code == 200
        body = response.json()
        assert body["blocked"] is False

    def test_requires_authentication(self) -> None:
        """Without the require_session override, the endpoint requires real auth."""
        app = FastAPI()
        app.include_router(admin_api.router)
        client = TestClient(app)

        # No auth dependency override → require_session enforces real auth.
        # In tests with no configured OIDC, require_session raises 401.
        response = client.post("/api/admin/cooldown/reset")
        assert response.status_code in (401, 503)
