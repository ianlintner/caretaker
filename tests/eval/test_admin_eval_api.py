"""Tests for :mod:`caretaker.admin.eval_api` + augmented shadow endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.admin import auth as admin_auth
from caretaker.admin import eval_api, shadow_api
from caretaker.eval import store
from caretaker.eval.harness import NightlyReport, ScorerSummary, SiteReport
from caretaker.evolution.shadow import (
    ShadowDecisionRecord,
    clear_records_for_tests,
    write_shadow_decision,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    clear_records_for_tests()
    store.clear_for_tests()
    shadow_api.configure(None)
    yield
    clear_records_for_tests()
    store.clear_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(shadow_api.router)
    app.include_router(eval_api.router)

    async def _fake_user() -> admin_auth.UserInfo:
        return admin_auth.UserInfo(sub="u", email="u@x", name="U", picture=None)

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    return TestClient(app)


def _seed_report(*, site: str, rate: float) -> None:
    until = datetime.now(UTC)
    since = until - timedelta(days=1)
    store.store_report(
        NightlyReport(
            since=since,
            until=until,
            sites=[
                SiteReport(
                    site=site,
                    record_count=10,
                    scorer_summaries=[
                        ScorerSummary(scorer="m", mean=rate, count=10),
                    ],
                    experiment_url="https://braintrust.test/exp/1",
                    braintrust_logged=True,
                )
            ],
        )
    )


class TestLatestEndpoint:
    def test_returns_404_when_no_report(self, client: TestClient) -> None:
        r = client.get("/api/admin/eval/latest", params={"site": "readiness"})
        assert r.status_code == 404

    def test_returns_site_summary(self, client: TestClient) -> None:
        _seed_report(site="readiness", rate=0.98)
        r = client.get("/api/admin/eval/latest", params={"site": "readiness"})
        assert r.status_code == 200
        data = r.json()
        assert data["site"] == "readiness"
        assert data["agreement_rate"] == pytest.approx(0.98)
        assert data["agreement_rate_7d"] == pytest.approx(0.98)
        assert data["experiment_url"] == "https://braintrust.test/exp/1"
        assert data["braintrust_logged"] is True

    def test_returns_404_for_unknown_site_in_report(self, client: TestClient) -> None:
        _seed_report(site="readiness", rate=0.98)
        r = client.get("/api/admin/eval/latest", params={"site": "cascade"})
        assert r.status_code == 404


class TestAugmentedShadowEndpoint:
    def test_attaches_agreement_rate_7d_when_name_pinned(self, client: TestClient) -> None:
        write_shadow_decision(
            ShadowDecisionRecord(
                id="r",
                name="readiness",
                repo_slug="a/b",
                run_at=datetime.now(UTC),
                outcome="agree",
                mode="shadow",
                legacy_verdict_json='{"v": 1}',
                candidate_verdict_json='{"v": 1}',
                disagreement_reason=None,
                context_json="{}",
            )
        )
        _seed_report(site="readiness", rate=0.97)
        r = client.get("/api/admin/shadow/decisions", params={"name": "readiness"})
        assert r.status_code == 200
        data = r.json()
        assert data["agreement_rate_7d"] == pytest.approx(0.97)
        assert data["agreement_rate_7d_by_site"] is None

    def test_unpinned_query_returns_per_site_map(self, client: TestClient) -> None:
        _seed_report(site="readiness", rate=0.97)
        r = client.get("/api/admin/shadow/decisions")
        assert r.status_code == 200
        data = r.json()
        assert data["agreement_rate_7d"] is None
        assert data["agreement_rate_7d_by_site"] == {
            "readiness": pytest.approx(0.97),
        }
