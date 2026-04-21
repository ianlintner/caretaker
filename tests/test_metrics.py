"""Prometheus metrics instrumentation tests.

Covers the paved-path SKILL non-negotiables (§1, §3, §4): RED-floor
metrics emit after a FastAPI request, histogram buckets match the
curated latency set, route labels are templated (not raw), and the
cardinality stays well under the 1000-series budget.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from caretaker.observability.metrics import (
    HTTP_SERVER_REQUEST_DURATION_SECONDS,
    HTTP_SERVER_REQUESTS_TOTAL,
    LATENCY_BUCKETS,
    REGISTRY,
    init_metrics,
)


def _build_instrumented_app() -> FastAPI:
    """Return a tiny FastAPI app with metrics instrumentation mounted.

    Running ``init_metrics`` outside the lifespan is deliberate — the
    lifespan handler spins up a background uvicorn server which we don't
    want in a synchronous ``TestClient`` context. The middleware
    registration is idempotent per app instance, so this is safe.
    """
    # Make sure the metrics server side-car stays off for tests.
    os.environ["CARETAKER_METRICS_PORT"] = "0"

    app = FastAPI()
    init_metrics(app, service="caretaker-test")

    @app.get("/api/fleet/heartbeat")
    async def _heartbeat() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/fleet/{client_id}")
    async def _client(client_id: str) -> dict[str, str]:
        return {"client_id": client_id}

    return app


def test_http_server_requests_total_increments_after_call() -> None:
    """After hitting the app once, ``http_server_requests_total`` appears."""
    app = _build_instrumented_app()
    client = TestClient(app)

    response = client.get("/api/fleet/heartbeat")
    assert response.status_code == 200

    # The Counter is keyed on (service, method, route, status). Read
    # back the series directly — no need to scrape /metrics for this.
    sample = HTTP_SERVER_REQUESTS_TOTAL.labels(
        service="caretaker-test",
        http_method="GET",
        http_route="/api/fleet/heartbeat",
        http_status_code="200",
    )
    assert sample._value.get() >= 1


def test_histogram_buckets_match_skill_section_3() -> None:
    """Histogram bucket edges equal the §3 curated latency set exactly."""
    expected = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    assert expected == LATENCY_BUCKETS

    metric = HTTP_SERVER_REQUEST_DURATION_SECONDS
    # ``_upper_bounds`` is set on the child observer, so inspect a child.
    child = metric.labels(
        service="caretaker-test",
        http_method="GET",
        http_route="/api/fleet/heartbeat",
        http_status_code="200",
    )
    # Drop the +Inf sentinel prometheus_client appends internally.
    observed = tuple(b for b in child._upper_bounds if b != float("inf"))
    assert observed == expected


def test_route_label_is_templated_not_raw_path() -> None:
    """Hitting ``/api/fleet/{client_id}`` records the template, not the path."""
    app = _build_instrumented_app()
    client = TestClient(app)

    response = client.get("/api/fleet/abc-123-xyz")
    assert response.status_code == 200

    # The templated series should exist.
    templated = HTTP_SERVER_REQUESTS_TOTAL.labels(
        service="caretaker-test",
        http_method="GET",
        http_route="/api/fleet/{client_id}",
        http_status_code="200",
    )
    assert templated._value.get() >= 1

    # The raw-path series must not exist (cardinality bomb — §4).
    raw_path = "/api/fleet/abc-123-xyz"
    for metric in REGISTRY.collect():
        if metric.name != "http_server_requests":  # Counter suffix is _total
            continue
        for sample in metric.samples:
            assert sample.labels.get("http_route") != raw_path


def test_cardinality_bound_under_1000_series() -> None:
    """After a handful of calls, total series count stays < 1000 (§4)."""
    app = _build_instrumented_app()
    client = TestClient(app)

    # Warm a spread of status codes / methods / routes to fan out labels.
    client.get("/api/fleet/heartbeat")
    client.get("/api/fleet/client-1")
    client.get("/api/fleet/client-2")
    client.get("/does-not-exist")

    total_series = 0
    for metric in REGISTRY.collect():
        total_series += len(metric.samples)
    assert total_series < 1000, f"metric cardinality blew budget: {total_series}"


def test_metrics_endpoint_served_on_metrics_asgi_app() -> None:
    """The dedicated ``metrics_asgi_app`` returns ``text/plain`` on ``/metrics``."""
    from caretaker.observability.metrics import metrics_asgi_app

    asgi = metrics_asgi_app()

    # Exercise the ASGI app inline with a captured send/receive pair.
    sent_messages: list[dict[str, object]] = []

    async def _send(msg: dict[str, object]) -> None:
        sent_messages.append(msg)

    async def _receive() -> dict[str, object]:  # pragma: no cover - unused
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "path": "/metrics", "method": "GET"}

    import asyncio

    asyncio.run(asgi(scope, _receive, _send))

    assert sent_messages[0]["type"] == "http.response.start"
    assert sent_messages[0]["status"] == 200
    headers = dict(sent_messages[0]["headers"])  # type: ignore[arg-type]
    assert any(b"text/plain" in v for k, v in headers.items() if k == b"content-type")


def test_db_client_metrics_register_via_timed_op() -> None:
    """The ``timed_op`` decorator records counter+histogram samples."""
    from caretaker.observability.metrics import (
        DB_CLIENT_OPERATIONS_TOTAL,
        timed_op,
    )

    @timed_op(db_system="redis", operation="ping")
    def _ping() -> str:
        return "PONG"

    for _ in range(3):
        _ping()

    series = DB_CLIENT_OPERATIONS_TOTAL.labels(
        service="caretaker-test",
        db_system="redis",
        db_operation="ping",
        outcome="success",
    )
    assert series._value.get() >= 3


def test_timed_op_records_failure_outcome() -> None:
    """A raised exception records ``outcome="failure"`` and re-raises."""
    from caretaker.observability.metrics import (
        DB_CLIENT_OPERATIONS_TOTAL,
        timed_op,
    )

    @timed_op(db_system="redis", operation="explode")
    def _boom() -> None:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        _boom()

    series = DB_CLIENT_OPERATIONS_TOTAL.labels(
        service="caretaker-test",
        db_system="redis",
        db_operation="explode",
        outcome="failure",
    )
    assert series._value.get() >= 1


def test_rate_limit_cooldown_gauge_updates_from_rate_limit_module() -> None:
    """`_publish_rate_limit_metrics` mirrors cooldown state onto the gauge."""
    from caretaker.github_client.rate_limit import (
        _publish_rate_limit_metrics,
        get_cooldown,
        reset_for_tests,
    )
    from caretaker.observability.metrics import RATE_LIMIT_COOLDOWN_SECONDS

    reset_for_tests()

    cooldown = get_cooldown()
    cooldown.mark_blocked(until=9999999999.0, reason="test")
    _publish_rate_limit_metrics()

    gauge = RATE_LIMIT_COOLDOWN_SECONDS.labels(service="caretaker-test", peer_service="github")
    assert gauge._value.get() > 0

    reset_for_tests()
