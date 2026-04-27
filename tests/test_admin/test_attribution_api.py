"""Tests for ``GET /api/admin/attribution/weekly``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import api as admin_api
from caretaker.admin import auth as admin_auth
from caretaker.admin.data import AdminDataAccess
from caretaker.state.models import (
    OrchestratorState,
    PRTrackingState,
    TrackedPR,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_api.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="u", email="u@example.com", name="U", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app)


def _seed(state: OrchestratorState) -> None:
    """Install an ``AdminDataAccess`` backed by ``state``."""
    admin_api.configure(AdminDataAccess(state=state))


def _now() -> datetime:
    return datetime.now(UTC)


class TestAttributionWeeklyEndpoint:
    def test_empty_state_returns_zeros(self, client: TestClient) -> None:
        _seed(OrchestratorState())
        response = client.get("/api/admin/attribution/weekly")
        assert response.status_code == 200
        body = response.json()
        assert body["touched"] == 0
        assert body["merged"] == 0
        assert body["operator_rescued"] == 0
        assert body["abandoned"] == 0
        assert body["avg_time_to_merge_hours"] is None

    def test_counts_merged_and_touched(self, client: TestClient) -> None:
        _seed(
            OrchestratorState(
                tracked_prs={
                    1: TrackedPR(
                        number=1,
                        caretaker_touched=True,
                        caretaker_merged=True,
                        state=PRTrackingState.MERGED,
                        first_seen_at=_now() - timedelta(hours=4),
                        merged_at=_now() - timedelta(hours=1),
                        last_checked=_now(),
                    ),
                    2: TrackedPR(
                        number=2,
                        caretaker_touched=True,
                        state=PRTrackingState.REVIEW_PENDING,
                        first_seen_at=_now() - timedelta(hours=2),
                        last_checked=_now(),
                    ),
                }
            )
        )
        response = client.get("/api/admin/attribution/weekly")
        assert response.status_code == 200
        body = response.json()
        assert body["touched"] == 2
        assert body["merged"] == 1
        # The merged PR was open for three hours; average rounds to 3.0
        assert body["avg_time_to_merge_hours"] == 3.0

    def test_counts_rescued_and_abandoned(self, client: TestClient) -> None:
        _seed(
            OrchestratorState(
                tracked_prs={
                    1: TrackedPR(
                        number=1,
                        caretaker_touched=True,
                        operator_intervened=True,
                        state=PRTrackingState.REVIEW_PENDING,
                        last_checked=_now(),
                    ),
                    2: TrackedPR(
                        number=2,
                        caretaker_touched=True,
                        state=PRTrackingState.ESCALATED,
                        last_checked=_now(),
                    ),
                }
            )
        )
        body = client.get("/api/admin/attribution/weekly").json()
        assert body["operator_rescued"] == 1
        assert body["abandoned"] == 1

    def test_since_window_excludes_old_rows(self, client: TestClient) -> None:
        # PR from 30 days ago; default window is 7 days, so it should be
        # excluded from the rollup.
        _seed(
            OrchestratorState(
                tracked_prs={
                    1: TrackedPR(
                        number=1,
                        caretaker_touched=True,
                        caretaker_merged=True,
                        state=PRTrackingState.MERGED,
                        first_seen_at=_now() - timedelta(days=31),
                        merged_at=_now() - timedelta(days=30),
                        last_checked=_now() - timedelta(days=30),
                    ),
                }
            )
        )
        body = client.get("/api/admin/attribution/weekly").json()
        assert body["touched"] == 0
        assert body["merged"] == 0

    def test_since_query_param_overrides_default(self, client: TestClient) -> None:
        _seed(
            OrchestratorState(
                tracked_prs={
                    1: TrackedPR(
                        number=1,
                        caretaker_touched=True,
                        state=PRTrackingState.REVIEW_PENDING,
                        last_checked=_now() - timedelta(days=20),
                    ),
                }
            )
        )
        since = (_now() - timedelta(days=30)).isoformat()
        response = client.get("/api/admin/attribution/weekly", params={"since": since})
        body = response.json()
        assert response.status_code == 200, body
        assert body["touched"] == 1

    def test_invalid_since_returns_400(self, client: TestClient) -> None:
        _seed(OrchestratorState())
        response = client.get("/api/admin/attribution/weekly?since=not-a-date")
        assert response.status_code == 400


class TestAttributionWeeklyRepoQuery:
    def test_repo_query_ignored_in_single_repo_mode(self, client: TestClient) -> None:
        _seed(
            OrchestratorState(
                tracked_prs={
                    1: TrackedPR(
                        number=1,
                        caretaker_touched=True,
                        state=PRTrackingState.REVIEW_PENDING,
                        last_checked=_now(),
                    ),
                }
            )
        )
        body = client.get("/api/admin/attribution/weekly?repo=foo/bar").json()
        assert body["touched"] == 1
