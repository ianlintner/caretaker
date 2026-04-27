"""Regression test: HTTP RED-floor metrics middleware is registered at
module import for the production ``caretaker-mcp`` app.

If ``init_metrics`` ever moves back inside the FastAPI lifespan handler,
Starlette will reject the late ``add_middleware`` call with
``RuntimeError: Cannot add middleware after an application has started``.
The existing ``mcp_backend.main`` masks that error as a warning, so the
pod stays up but ``caretaker_http_server_requests_total`` never
increments. This test catches that regression by hitting ``/health``
through a TestClient (which exercises the real lifespan) and asserting
the counter actually moved.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient


def test_health_request_increments_caretaker_mcp_counter() -> None:
    """A live request to the production app advances the RED counter."""
    # Disable the background /metrics uvicorn sidecar — it would try to
    # bind 9090 and fight any other test runner.
    os.environ["CARETAKER_METRICS_PORT"] = "0"

    from caretaker.mcp_backend.main import app
    from caretaker.observability.metrics import HTTP_SERVER_REQUESTS_TOTAL

    series = HTTP_SERVER_REQUESTS_TOTAL.labels(
        service="caretaker-mcp",
        http_method="GET",
        http_route="/health",
        http_status_code="200",
    )
    before = series._value.get()

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    after = series._value.get()
    assert after == before + 1, (
        "HTTP_SERVER_REQUESTS_TOTAL did not increment after GET /health — "
        "the metrics middleware is probably not registered. Check that "
        "caretaker.mcp_backend.main calls init_metrics(app, ...) at module "
        "scope (not from inside _lifespan)."
    )
