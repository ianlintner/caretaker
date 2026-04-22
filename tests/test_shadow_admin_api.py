"""Tests for ``GET /api/admin/shadow/decisions``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import auth as admin_auth
from caretaker.admin import shadow_api
from caretaker.evolution.shadow import (
    ShadowDecisionRecord,
    clear_records_for_tests,
    write_shadow_decision,
)
from caretaker.graph import writer as graph_writer


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    graph_writer.reset_for_tests()
    shadow_api.configure(None)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(shadow_api.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="u", email="u@example.com", name="U", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app)


def _record(
    *,
    rid: str = "rec-1",
    name: str = "readiness",
    outcome: str = "agree",
    repo_slug: str = "ian/demo",
    minutes_ago: int = 0,
) -> ShadowDecisionRecord:
    return ShadowDecisionRecord(
        id=rid,
        name=name,
        repo_slug=repo_slug,
        run_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        outcome=outcome,  # type: ignore[arg-type]
        mode="shadow",
        legacy_verdict_json='{"v": 1}',
        candidate_verdict_json='{"v": 2}' if outcome == "disagree" else '{"v": 1}',
        disagreement_reason="mismatch" if outcome == "disagree" else None,
        context_json='{"pr": 1}',
    )


class TestShadowAdminAPI:
    def test_ring_buffer_fallback_empty(self, client: TestClient) -> None:
        resp = client.get("/api/admin/shadow/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["agreement_rate"] == 1.0
        assert data["source"] == "ring_buffer"

    def test_ring_buffer_returns_newest_first(self, client: TestClient) -> None:
        write_shadow_decision(_record(rid="old", minutes_ago=10))
        write_shadow_decision(_record(rid="new", minutes_ago=0))

        resp = client.get("/api/admin/shadow/decisions")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["id"] for i in items] == ["new", "old"]

    def test_name_filter(self, client: TestClient) -> None:
        write_shadow_decision(_record(rid="a", name="readiness"))
        write_shadow_decision(_record(rid="b", name="ci_triage"))

        resp = client.get("/api/admin/shadow/decisions?name=ci_triage")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["id"] for i in items] == ["b"]

    def test_since_filter(self, client: TestClient) -> None:
        write_shadow_decision(_record(rid="old", minutes_ago=60))
        write_shadow_decision(_record(rid="recent", minutes_ago=1))

        cutoff = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        # Pass via ``params`` so the ``+00:00`` suffix gets URL-encoded
        # instead of surfacing as a literal space.
        resp = client.get("/api/admin/shadow/decisions", params={"since": cutoff})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [i["id"] for i in items] == ["recent"]

    def test_agreement_rate_mixed(self, client: TestClient) -> None:
        write_shadow_decision(_record(rid="a1", outcome="agree"))
        write_shadow_decision(_record(rid="a2", outcome="agree"))
        write_shadow_decision(_record(rid="a3", outcome="agree"))
        write_shadow_decision(_record(rid="d1", outcome="disagree"))
        # candidate_error rows must be excluded from the denominator.
        write_shadow_decision(_record(rid="e1", outcome="candidate_error"))

        resp = client.get("/api/admin/shadow/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agreement_rate"] == pytest.approx(0.75)

    def test_limit_capped(self, client: TestClient) -> None:
        for i in range(10):
            write_shadow_decision(_record(rid=f"r{i}"))
        resp = client.get("/api/admin/shadow/decisions?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3

    def test_neo4j_backend_used_when_configured(self, client: TestClient) -> None:
        class _FakeSession:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._rows = rows

            async def __aenter__(self) -> _FakeSession:
                return self

            async def __aexit__(self, *exc: object) -> None:
                return None

            async def run(self, query: str, **params: Any) -> _FakeResult:  # noqa: ARG002
                return _FakeResult(self._rows)

        class _FakeResult:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._rows = rows

            def __aiter__(self) -> _FakeResult:
                self._iter = iter(self._rows)
                return self

            async def __anext__(self) -> dict[str, Any]:
                try:
                    return next(self._iter)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

        class _FakeDriver:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._rows = rows

            def session(self, *, database: str) -> _FakeSession:  # noqa: ARG002
                return _FakeSession(self._rows)

        class _FakeGraphStore:
            def __init__(self, rows: list[dict[str, Any]]) -> None:
                self._driver = _FakeDriver(rows)
                self._database = "caretaker"

        now = datetime.now(UTC).isoformat()
        rows = [
            {
                "s": {
                    "id": "node-1",
                    "name": "readiness",
                    "repo_slug": "ian/demo",
                    "run_at": now,
                    "outcome": "disagree",
                    "mode": "shadow",
                    "legacy_verdict_json": '{"ready": true}',
                    "candidate_verdict_json": '{"ready": false}',
                    "disagreement_reason": "verdict mismatch",
                    "context_json": '{"pr": 1}',
                }
            },
        ]
        shadow_api.configure(_FakeGraphStore(rows))

        resp = client.get("/api/admin/shadow/decisions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "neo4j"
        assert len(data["items"]) == 1
        assert data["items"][0]["id"] == "node-1"
        assert data["items"][0]["outcome"] == "disagree"
        # agreement_rate over one disagreement = 0.
        assert data["agreement_rate"] == 0.0

    def test_requires_authenticated_session(self) -> None:
        """Endpoint is protected by ``require_session`` (no override → 401)."""
        app = FastAPI()
        app.include_router(shadow_api.router)
        # No dependency override — require_session fires and should 401.
        # But the underlying helper needs _signer/_redis; when those are
        # not configured, require_session raises 401 before getting there.
        client = TestClient(app)
        resp = client.get("/api/admin/shadow/decisions")
        assert resp.status_code == 401
