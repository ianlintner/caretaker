"""Integration test for ``GET /health/deep`` on the MCP backend FastAPI app.

Stubs ``gather_deep_health`` (covered by unit tests in
``test_observability_health.py``) so the endpoint test focuses on:

  * 200 status code with ``status=ok``
  * 503 status code with ``status=fail``
  * Body shape matches the documented JSON contract
  * Config-load failure surfaces as ``checks.config.status = "fail"``
  * Wall-time timeout returns 503 with a structured error
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from caretaker.mcp_backend.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _stub_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't actually open Mongo/Neo4j clients — env vars stay blank."""
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("NEO4J_URL", raising=False)


def test_health_deep_returns_200_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_gather(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.28.4",
            "checks": {
                "git_cli": {"status": "ok", "version": "2.47.3"},
                "opencode_cli": {"status": "ok", "version": "1.14.39"},
                "redis": {"status": "ok", "latency_ms": 1},
                "mongo": {"status": "ok", "latency_ms": 1},
                "neo4j": {"status": "ok", "latency_ms": 1},
                "openrouter_models": {
                    "status": "ok",
                    "probed_at": "2026-05-06T00:00:00+00:00",
                    "results": {},
                },
            },
        }

    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _fake_gather)

    response = client.get("/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "checks" in body
    assert set(body["checks"].keys()) == {
        "git_cli",
        "opencode_cli",
        "redis",
        "mongo",
        "neo4j",
        "openrouter_models",
    }


def test_health_deep_returns_503_on_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_gather(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "fail",
            "version": "0.28.4",
            "checks": {
                "git_cli": {"status": "fail", "error": "git binary not found on PATH"},
                "opencode_cli": {"status": "ok", "version": "1.14.39"},
                "redis": {"status": "ok", "latency_ms": 1},
                "mongo": {"status": "ok", "latency_ms": 1},
                "neo4j": {"status": "ok", "latency_ms": 1},
                "openrouter_models": {"status": "skipped", "results": {}},
            },
        }

    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _fake_gather)

    response = client.get("/health/deep")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert body["checks"]["git_cli"]["status"] == "fail"


def test_health_deep_returns_200_on_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A degraded model probe with healthy core stack is still 200."""

    async def _fake_gather(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "degraded",
            "version": "0.28.4",
            "checks": {
                "git_cli": {"status": "ok", "version": "2.47.3"},
                "opencode_cli": {"status": "ok", "version": "1.14.39"},
                "redis": {"status": "ok", "latency_ms": 1},
                "mongo": {"status": "ok", "latency_ms": 1},
                "neo4j": {"status": "ok", "latency_ms": 1},
                "openrouter_models": {
                    "status": "fail",
                    "probed_at": "2026-05-06T00:00:00+00:00",
                    "results": {"openrouter/m1": "fail: No endpoints found"},
                },
            },
        }

    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _fake_gather)

    response = client.get("/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"


def test_health_deep_caches_model_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two back-to-back calls hit the model-probe cache; subprocess fires once."""
    import asyncio as _asyncio

    from caretaker.observability.health import _reset_cache_for_tests

    _reset_cache_for_tests()

    call_counter = {"n": 0}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"pong\n", b""

        async def wait(self) -> int:
            return 0

    async def _factory(*args: Any, **_kwargs: Any) -> _FakeProc:
        # Only count "opencode run ping ..." spawns — git/opencode --version
        # are still probed live on every request.
        cmd = [str(a) for a in args]
        if "run" in cmd and "ping" in cmd:
            call_counter["n"] += 1
        return _FakeProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _factory)

    # First call cold-loads the cache.
    r1 = client.get("/health/deep")
    # Second call should hit the cache — model probe count must not increase.
    n_after_first = call_counter["n"]
    r2 = client.get("/health/deep")
    assert call_counter["n"] == n_after_first
    # Both responses should be valid (whatever the body says).
    assert r1.status_code in (200, 503)
    assert r2.status_code in (200, 503)


def test_health_deep_missing_config_file_is_ok_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A missing CARETAKER_CONFIG_PATH is OK (defaults are used).

    The MCP backend pod doesn't ship a consumer-side config YAML — it
    uses defaults + env vars. Treating "no file" as a hard failure was
    a false alarm in v0.29.0 deploy. Now ``resolve_models`` returns
    ``status="ok"`` with a ``note`` field instead, and the endpoint
    returns 200.
    """
    missing = tmp_path / "definitely-not-here.yml"
    monkeypatch.setenv("CARETAKER_CONFIG_PATH", str(missing))

    async def _fake_gather(**kwargs: Any) -> dict[str, Any]:
        config_check = kwargs.get("config_check")
        assert config_check is not None
        assert config_check["status"] == "ok"
        assert "note" in config_check
        assert "not present" in config_check["note"]
        return {
            "status": "ok",
            "version": "0.29.0",
            "checks": {
                "config": config_check,
                "git_cli": {"status": "ok", "version": "2.47.3"},
                "opencode_cli": {"status": "ok", "version": "1.14.39"},
                "redis": {"status": "ok", "latency_ms": 1},
                "mongo": {"status": "ok", "latency_ms": 1},
                "neo4j": {"status": "ok", "latency_ms": 1},
                "openrouter_models": {"status": "ok", "results": {}},
            },
        }

    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _fake_gather)

    response = client.get("/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["config"]["status"] == "ok"


def test_health_deep_returns_503_on_malformed_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A malformed YAML at CARETAKER_CONFIG_PATH IS still a hard fail.

    Distinct from the "no file at all" case above — if the operator
    DID mount a config file and it's broken, that's a real deploy bug.
    """
    bad_yaml = tmp_path / "config.yml"
    bad_yaml.write_text("this: : is : invalid : yaml :\n  - 1\n  - 2\n  3")
    monkeypatch.setenv("CARETAKER_CONFIG_PATH", str(bad_yaml))

    async def _fake_gather(**kwargs: Any) -> dict[str, Any]:
        config_check = kwargs.get("config_check")
        assert config_check is not None
        assert config_check["status"] == "fail"
        return {
            "status": "fail",
            "version": "0.29.0",
            "checks": {
                "config": config_check,
                "git_cli": {"status": "ok", "version": "2.47.3"},
                "opencode_cli": {"status": "ok", "version": "1.14.39"},
                "redis": {"status": "ok", "latency_ms": 1},
                "mongo": {"status": "ok", "latency_ms": 1},
                "neo4j": {"status": "ok", "latency_ms": 1},
                "openrouter_models": {"status": "skipped", "results": {}},
            },
        }

    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _fake_gather)

    response = client.get("/health/deep")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert body["checks"]["config"]["status"] == "fail"


def test_health_deep_returns_503_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``gather_deep_health`` exceeds the wall-time budget, return 503."""

    async def _slow_gather(**_kwargs: Any) -> dict[str, Any]:
        # Sleep long enough that asyncio.wait_for will trip first. The
        # endpoint patches the budget below to keep the test fast.
        await asyncio.sleep(5)
        return {"status": "ok", "version": "0.28.4", "checks": {}}

    import caretaker.mcp_backend.main as main_module
    import caretaker.observability.health as health_module

    monkeypatch.setattr(health_module, "gather_deep_health", _slow_gather)
    monkeypatch.setattr(main_module, "HEALTH_DEEP_TIMEOUT_SECONDS", 0.05)

    response = client.get("/health/deep")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "fail"
    assert "timed out" in body["error"]
