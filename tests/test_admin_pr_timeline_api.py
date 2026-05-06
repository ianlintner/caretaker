"""Tests for ``GET /api/admin/pr/{owner}/{repo}/{number}/timeline``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import auth as admin_auth
from caretaker.admin import pr_timeline_api
from caretaker.state.pr_decisions import PRDecisionStore

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def sort(self, key: str, direction: int) -> _FakeCursor:
        reverse = direction < 0
        self._docs = sorted(self._docs, key=lambda d: d[key], reverse=reverse)
        return self

    def limit(self, n: int) -> _FakeCursor:
        self._docs = self._docs[: int(n)]
        return self

    def __aiter__(self) -> _FakeCursor:
        self._iter = iter(self._docs)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []
        self.create_index = AsyncMock(return_value="idx")

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(doc)

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        matched = [d for d in self._docs if all(d.get(k) == v for k, v in query.items())]
        return _FakeCursor(matched)


def _build_store() -> tuple[PRDecisionStore, _FakeCollection]:
    """Create a PRDecisionStore wired to an in-memory fake collection."""
    store = PRDecisionStore(enabled=True)
    col = _FakeCollection()

    async def _stub() -> _FakeCollection:
        return col

    store._ensure_collection = _stub  # type: ignore[assignment]
    return store, col


@pytest.fixture
def store_and_col() -> Iterator[tuple[PRDecisionStore, _FakeCollection]]:
    store, col = _build_store()
    pr_timeline_api.configure(store)
    yield store, col
    pr_timeline_api.configure(None)


@pytest.fixture
def client_authed(
    store_and_col: tuple[PRDecisionStore, _FakeCollection],
) -> TestClient:
    app = FastAPI()
    app.include_router(pr_timeline_api.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="u", email="u@example.com", name="U", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app)


@pytest.fixture
def client_unauthed(
    store_and_col: tuple[PRDecisionStore, _FakeCollection],
) -> TestClient:
    app = FastAPI()
    app.include_router(pr_timeline_api.router)
    return TestClient(app)


class TestPRTimelineAPI:
    def test_get_timeline_returns_decisions(
        self,
        client_authed: TestClient,
        store_and_col: tuple[PRDecisionStore, _FakeCollection],
    ) -> None:
        _store, col = store_and_col
        now = datetime.now(UTC)
        # Two decisions, oldest first.
        col._docs = [
            {
                "id": "d1",
                "repo": "ianlintner/caretaker-qa",
                "pr_number": 108,
                "observed_at": now - timedelta(minutes=5),
                "agent": "pr_reviewer",
                "event": "review_started",
                "fields": {"author": "ianlintner", "is_caretaker_owned": False},
                "trace_id": "a" * 32,
                "span_id": "b" * 16,
            },
            {
                "id": "d2",
                "repo": "ianlintner/caretaker-qa",
                "pr_number": 108,
                "observed_at": now - timedelta(minutes=1),
                "agent": "complexity_classifier",
                "event": "tier_classified",
                "fields": {"tier": "simple", "source": "fast_path", "confidence": 0.9},
                "trace_id": None,
                "span_id": None,
            },
        ]

        resp = client_authed.get("/api/admin/pr/ianlintner/caretaker-qa/108/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["owner"] == "ianlintner"
        assert data["repo"] == "caretaker-qa"
        assert data["pr_number"] == 108
        assert len(data["decisions"]) == 2
        events = [d["event"] for d in data["decisions"]]
        assert events == ["review_started", "tier_classified"]
        assert data["decisions"][0]["fields"]["author"] == "ianlintner"
        assert data["decisions"][0]["trace_id"] == "a" * 32

    def test_get_timeline_returns_empty_when_no_decisions(
        self,
        client_authed: TestClient,
    ) -> None:
        # No documents inserted into the fake collection. Endpoint returns
        # an empty list rather than a 404 — matches other admin APIs that
        # return ``[]`` for "no data" so the SPA can render an empty state.
        resp = client_authed.get("/api/admin/pr/anyone/anywhere/999/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["decisions"] == []
        assert data["pr_number"] == 999

    def test_get_timeline_unauthorized(
        self,
        client_unauthed: TestClient,
    ) -> None:
        # Without the dependency override, the endpoint should 401/403.
        resp = client_unauthed.get("/api/admin/pr/ianlintner/caretaker-qa/108/timeline")
        assert resp.status_code in (401, 403, 503)
