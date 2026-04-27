"""Tests for the T-E4 :FleetAlert evaluator + admin endpoint.

Covers:

* Each alert ``kind`` trips on the right heartbeat shape.
* Deduplication: re-running the evaluator on the same batch does not
  double-emit. The admin-side alert store preserves ``opened_at`` across
  re-evaluation.
* Resolution flow: a metric that clears threshold flips ``resolved_at``
  and drops out of the ``open=true`` listing.
* Admin endpoint ``GET /api/admin/fleet/alerts?open=true`` behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.config import FleetAlertConfig, FleetConfig, MaintainerConfig
from caretaker.fleet import (
    FleetAlert,
    FleetAlertStore,
    FleetHeartbeat,
    admin_router,
    evaluate_fleet_alerts,
    get_alert_store,
    public_router,
    reset_alert_store_for_tests,
    reset_store_for_tests,
    set_fleet_alert_dependencies,
    upsert_fleet_alerts,
)
from caretaker.graph.models import NodeType


def _hb(
    repo: str,
    *,
    run_at: datetime,
    goal_health: float | None = None,
    error_count: int = 0,
    summary: dict[str, Any] | None = None,
) -> FleetHeartbeat:
    return FleetHeartbeat(
        repo=repo,
        caretaker_version="0.14.0",
        run_at=run_at,
        mode="full",
        enabled_agents=["pr_agent"],
        goal_health=goal_health,
        error_count=error_count,
        counters={},
        summary=summary,
    )


_T0 = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)


# ── Config defaults ──────────────────────────────────────────────────────


def test_fleet_alert_config_defaults() -> None:
    cfg = MaintainerConfig()
    assert cfg.fleet.alerts.enabled is False
    assert cfg.fleet.alerts.goal_health_threshold == pytest.approx(0.7)
    assert cfg.fleet.alerts.goal_health_n_consecutive == 3
    assert cfg.fleet.alerts.error_spike_multiplier == pytest.approx(3.0)
    assert cfg.fleet.alerts.ghosted_window_days == 7


def test_fleet_alert_config_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    yaml_path = tmp_path / "config.yml"
    yaml_path.write_text(
        "version: v1\n"
        "fleet:\n"
        "  alerts:\n"
        "    enabled: true\n"
        "    goal_health_threshold: 0.8\n"
        "    goal_health_n_consecutive: 2\n"
    )
    cfg = MaintainerConfig.from_yaml(yaml_path)
    assert cfg.fleet.alerts.enabled is True
    assert cfg.fleet.alerts.goal_health_threshold == pytest.approx(0.8)
    assert cfg.fleet.alerts.goal_health_n_consecutive == 2


# ── evaluate_fleet_alerts — per-kind tripping ───────────────────────────


def test_goal_health_regression_trips_on_n_consecutive_low() -> None:
    hbs = [
        _hb("a/b", run_at=_T0 - timedelta(hours=3), goal_health=0.9),
        _hb("a/b", run_at=_T0 - timedelta(hours=2), goal_health=0.4),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), goal_health=0.5),
        _hb("a/b", run_at=_T0, goal_health=0.6),
    ]
    alerts = evaluate_fleet_alerts(
        hbs,
        goal_health_threshold=0.7,
        goal_health_n_consecutive=3,
        now=_T0,
    )
    kinds = [a.kind for a in alerts]
    assert "goal_health_regression" in kinds
    gh = next(a for a in alerts if a.kind == "goal_health_regression")
    assert gh.repo == "a/b"
    assert gh.details["samples"] == [0.4, 0.5, 0.6]


def test_goal_health_regression_does_not_trip_when_any_sample_recovers() -> None:
    hbs = [
        _hb("a/b", run_at=_T0 - timedelta(hours=2), goal_health=0.4),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), goal_health=0.9),  # recovery
        _hb("a/b", run_at=_T0, goal_health=0.5),
    ]
    alerts = evaluate_fleet_alerts(
        hbs,
        goal_health_threshold=0.7,
        goal_health_n_consecutive=3,
        now=_T0,
    )
    assert not any(a.kind == "goal_health_regression" for a in alerts)


def test_error_spike_trips_on_sudden_jump() -> None:
    hbs = [
        _hb("a/b", run_at=_T0 - timedelta(hours=3), error_count=0),
        _hb("a/b", run_at=_T0 - timedelta(hours=2), error_count=0),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), error_count=1),
        _hb("a/b", run_at=_T0, error_count=10),  # big jump
    ]
    alerts = evaluate_fleet_alerts(hbs, error_spike_multiplier=3.0, now=_T0)
    spike = next(a for a in alerts if a.kind == "error_spike")
    assert spike.repo == "a/b"
    assert spike.details["latest_error_count"] == 10


def test_error_spike_floors_mean_at_one_so_zero_to_three_trips() -> None:
    hbs = [
        _hb("a/b", run_at=_T0 - timedelta(hours=2), error_count=0),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), error_count=0),
        _hb("a/b", run_at=_T0, error_count=3),
    ]
    alerts = evaluate_fleet_alerts(hbs, error_spike_multiplier=3.0, now=_T0)
    assert any(a.kind == "error_spike" for a in alerts)


def test_ghosted_trips_when_last_seen_older_than_window() -> None:
    hbs = [_hb("a/b", run_at=_T0 - timedelta(days=10), goal_health=0.9)]
    alerts = evaluate_fleet_alerts(hbs, ghosted_window_days=7, now=_T0)
    ghosted = next(a for a in alerts if a.kind == "ghosted")
    assert ghosted.repo == "a/b"
    assert ghosted.details["age_days"] == 10


def test_ghosted_does_not_trip_inside_window() -> None:
    hbs = [_hb("a/b", run_at=_T0 - timedelta(days=3), goal_health=0.9)]
    alerts = evaluate_fleet_alerts(hbs, ghosted_window_days=7, now=_T0)
    assert not any(a.kind == "ghosted" for a in alerts)


def test_scope_gap_trips_on_flag_in_summary() -> None:
    hbs = [
        _hb(
            "a/b",
            run_at=_T0,
            goal_health=0.9,
            summary={"scope_gap_open": True, "scope_gap_count": 2},
        )
    ]
    alerts = evaluate_fleet_alerts(hbs, now=_T0)
    assert any(a.kind == "scope_gap" for a in alerts)


def test_scope_gap_trips_on_nested_shape() -> None:
    hbs = [
        _hb(
            "a/b",
            run_at=_T0,
            goal_health=0.9,
            summary={"scope_gap": {"open": True, "scope_hint": "workflow", "count": 3}},
        )
    ]
    alerts = evaluate_fleet_alerts(hbs, now=_T0)
    scope_gap = next(a for a in alerts if a.kind == "scope_gap")
    assert "workflow" in scope_gap.summary


def test_scope_gap_quiet_when_no_flag() -> None:
    hbs = [_hb("a/b", run_at=_T0, goal_health=0.9, summary={"other": 1})]
    alerts = evaluate_fleet_alerts(hbs, now=_T0)
    assert not any(a.kind == "scope_gap" for a in alerts)


# ── Deduplication ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_twice_yields_same_alert_set_and_store_dedups() -> None:
    store = FleetAlertStore()
    hbs = [
        _hb("a/b", run_at=_T0 - timedelta(hours=2), goal_health=0.3),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), goal_health=0.4),
        _hb("a/b", run_at=_T0, goal_health=0.5),
    ]
    first = evaluate_fleet_alerts(hbs, now=_T0)
    second = evaluate_fleet_alerts(hbs, now=_T0)
    assert [a.model_dump() for a in first] == [a.model_dump() for a in second]

    merged_a = await store.apply(first)
    merged_b = await store.apply(second, now=_T0 + timedelta(minutes=1))
    # Same set of alerts; opened_at from the first apply is preserved,
    # no ``resolved_at`` populated because the metric still trips.
    assert len(merged_a) == len(merged_b) == 1
    assert merged_a[0].opened_at == merged_b[0].opened_at
    assert merged_b[0].resolved_at is None


# ── Resolution flow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolution_populates_resolved_at_and_admin_omits_when_open_only() -> None:
    store = FleetAlertStore()
    hbs_bad = [
        _hb("a/b", run_at=_T0 - timedelta(hours=2), goal_health=0.3),
        _hb("a/b", run_at=_T0 - timedelta(hours=1), goal_health=0.4),
        _hb("a/b", run_at=_T0, goal_health=0.5),
    ]
    alerts_bad = evaluate_fleet_alerts(hbs_bad, now=_T0)
    assert any(a.kind == "goal_health_regression" for a in alerts_bad)
    await store.apply(alerts_bad, now=_T0)

    # Next pass: metric recovers → evaluator emits nothing → store flips
    # resolved_at on the previously-open alert.
    hbs_ok = [
        _hb("a/b", run_at=_T0, goal_health=0.5),
        _hb("a/b", run_at=_T0 + timedelta(hours=1), goal_health=0.9),
        _hb("a/b", run_at=_T0 + timedelta(hours=2), goal_health=0.95),
    ]
    alerts_ok = evaluate_fleet_alerts(hbs_ok, now=_T0 + timedelta(hours=2))
    assert not any(a.kind == "goal_health_regression" for a in alerts_ok)
    merged = await store.apply(alerts_ok, now=_T0 + timedelta(hours=2))

    gh = next(a for a in merged if a.kind == "goal_health_regression")
    assert gh.resolved_at is not None

    open_rows = await store.list(open_only=True)
    assert not any(a.kind == "goal_health_regression" for a in open_rows)
    all_rows = await store.list(open_only=False)
    assert any(a.kind == "goal_health_regression" for a in all_rows)


# ── upsert_fleet_alerts — graph mirror ──────────────────────────────────


class _RecordingGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        self.calls.append((label, node_id, properties))


@pytest.mark.asyncio
async def test_upsert_fleet_alerts_writes_fleet_alert_nodes() -> None:
    graph = _RecordingGraph()
    alerts = [
        FleetAlert(
            repo="a/b",
            kind="goal_health_regression",
            severity="warning",
            summary="goal_health low",
            opened_at=_T0,
        ),
    ]
    upsert_fleet_alerts(alerts, graph=graph)
    # upsert schedules a task; wait a tick for it to run on the current loop.
    import asyncio as _asyncio

    for _ in range(10):
        if graph.calls:
            break
        await _asyncio.sleep(0)
    assert graph.calls, "merge_node was not called"
    label, node_id, props = graph.calls[0]
    assert label == NodeType.FLEET_ALERT
    assert node_id == "fleet_alert:a_b:goal_health_regression"
    assert props["status"] == "open"
    assert props["resolved_at"] is None


# ── Admin endpoint ──────────────────────────────────────────────────────


@pytest.fixture
def fleet_alerts_client(monkeypatch):  # type: ignore[no-untyped-def]
    from caretaker.admin import auth as admin_auth
    from caretaker.auth.bearer import BearerPrincipal
    from caretaker.fleet import api as fleet_api

    reset_store_for_tests()
    reset_alert_store_for_tests()
    monkeypatch.delenv("CARETAKER_FLEET_SECRET", raising=False)
    # Enable the evaluator via an injected config so the endpoint exercises
    # the full path (not the short-circuit "disabled" branch).
    set_fleet_alert_dependencies(
        maintainer_config=MaintainerConfig(
            fleet=FleetConfig(alerts=FleetAlertConfig(enabled=True)),
        ),
        graph_store=None,
    )

    app = FastAPI()
    app.include_router(public_router)
    app.include_router(admin_router)

    async def _fake_user():  # noqa: ANN202
        return admin_auth.UserInfo(sub="test", email="test@example.com", name="Test", picture=None)

    async def _fake_principal():  # noqa: ANN202
        return BearerPrincipal(
            client_id="test-client",
            scopes=frozenset({"fleet:heartbeat"}),
            raw_claims={"client_id": "test-client", "scope": "fleet:heartbeat"},
        )

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    app.dependency_overrides[fleet_api._REQUIRE_FLEET_TOKEN.dependency] = _fake_principal
    yield TestClient(app)
    set_fleet_alert_dependencies(maintainer_config=None, graph_store=None)


def _post_heartbeat(
    client: TestClient,
    repo: str,
    *,
    run_at: datetime,
    goal_health: float | None = None,
    error_count: int = 0,
    summary: dict[str, Any] | None = None,
) -> None:
    body = {
        "repo": repo,
        "caretaker_version": "0.14.0",
        "run_at": run_at.isoformat(),
        "mode": "full",
        "enabled_agents": ["pr_agent"],
        "goal_health": goal_health,
        "error_count": error_count,
        "counters": {},
    }
    if summary is not None:
        body["summary"] = summary
    resp = client.post("/api/fleet/heartbeat", json=body)
    assert resp.status_code == 200, resp.text


def test_admin_alerts_endpoint_returns_open_only_when_flag_set(  # type: ignore[no-untyped-def]
    fleet_alerts_client,
) -> None:
    client = fleet_alerts_client
    base = _T0
    # Three consecutive low goal_health heartbeats trip the alert.
    for i, score in enumerate([0.3, 0.4, 0.5]):
        _post_heartbeat(
            client,
            "a/b",
            run_at=base + timedelta(hours=i),
            goal_health=score,
        )

    resp = client.get("/api/admin/fleet/alerts?open=true")
    assert resp.status_code == 200
    items = resp.json()["items"]
    kinds = [a["kind"] for a in items]
    assert "goal_health_regression" in kinds
    assert all(a["resolved_at"] is None for a in items)

    # Without open=true, resolved rows would also appear — we haven't
    # resolved anything yet so the set is identical.
    resp_all = client.get("/api/admin/fleet/alerts")
    assert resp_all.status_code == 200
    assert len(resp_all.json()["items"]) == len(items)


def test_admin_alerts_endpoint_resolution_flow(  # type: ignore[no-untyped-def]
    fleet_alerts_client,
) -> None:
    client = fleet_alerts_client
    base = _T0
    for i, score in enumerate([0.3, 0.4, 0.5]):
        _post_heartbeat(
            client,
            "a/b",
            run_at=base + timedelta(hours=i),
            goal_health=score,
        )
    # Trigger one evaluation so the alert is recorded in the store.
    first = client.get("/api/admin/fleet/alerts?open=true").json()
    assert any(a["kind"] == "goal_health_regression" for a in first["items"])

    # Now send three healthy heartbeats — this pushes the bad ones out of
    # the last-N window AND adds fresh high-score samples so the evaluator
    # no longer trips. The store flips resolved_at.
    for i, score in enumerate([0.9, 0.95, 0.92]):
        _post_heartbeat(
            client,
            "a/b",
            run_at=base + timedelta(hours=10 + i),
            goal_health=score,
        )

    resolved = client.get("/api/admin/fleet/alerts?open=true").json()
    assert not any(a["kind"] == "goal_health_regression" for a in resolved["items"])

    all_rows = client.get("/api/admin/fleet/alerts").json()
    gh = next(a for a in all_rows["items"] if a["kind"] == "goal_health_regression")
    assert gh["resolved_at"] is not None


def test_admin_alerts_endpoint_disabled_returns_stored_state_only(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """When config.fleet.alerts.enabled is False the endpoint must not
    re-evaluate — it just returns whatever the alert store already has.
    This is the default posture for the feature."""
    from caretaker.admin import auth as admin_auth
    from caretaker.auth.bearer import BearerPrincipal
    from caretaker.fleet import api as fleet_api

    reset_store_for_tests()
    reset_alert_store_for_tests()
    monkeypatch.delenv("CARETAKER_FLEET_SECRET", raising=False)
    set_fleet_alert_dependencies(
        maintainer_config=MaintainerConfig(),  # default: alerts disabled
        graph_store=None,
    )
    app = FastAPI()
    app.include_router(public_router)
    app.include_router(admin_router)

    async def _fake_user():  # noqa: ANN202
        return admin_auth.UserInfo(sub="test", email="test@example.com", name="Test", picture=None)

    async def _fake_principal():  # noqa: ANN202
        return BearerPrincipal(
            client_id="test-client",
            scopes=frozenset({"fleet:heartbeat"}),
            raw_claims={"client_id": "test-client", "scope": "fleet:heartbeat"},
        )

    app.dependency_overrides[admin_auth.require_session] = _fake_user
    app.dependency_overrides[fleet_api._REQUIRE_FLEET_TOKEN.dependency] = _fake_principal
    with TestClient(app) as client:
        for i, score in enumerate([0.3, 0.4, 0.5]):
            _post_heartbeat(
                client,
                "a/b",
                run_at=_T0 + timedelta(hours=i),
                goal_health=score,
            )
        # Alerts disabled → evaluator does not run → store is empty.
        resp = client.get("/api/admin/fleet/alerts")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    set_fleet_alert_dependencies(maintainer_config=None, graph_store=None)


# ── FleetAlert model ────────────────────────────────────────────────────


def test_fleet_alert_summary_max_length() -> None:
    with pytest.raises(ValueError):
        FleetAlert(
            repo="a/b",
            kind="ghosted",
            severity="warning",
            summary="x" * 241,
            opened_at=_T0,
        )


def test_fleet_alert_is_open() -> None:
    a = FleetAlert(repo="a/b", kind="ghosted", severity="warning", summary="x", opened_at=_T0)
    assert a.is_open()
    a.resolved_at = _T0
    assert not a.is_open()


def test_get_alert_store_singleton_survives_test_reset() -> None:
    store = get_alert_store()
    reset_alert_store_for_tests()
    assert get_alert_store() is not store
