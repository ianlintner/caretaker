"""Unit tests for :mod:`caretaker.observability.health`.

Covers each of the six dependency-check helpers, the cache behaviour of
``probe_openrouter_models`` (cold + warm), and the rollup logic in
``gather_deep_health``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from caretaker.observability import health as health_module
from caretaker.observability.health import (
    _reset_cache_for_tests,
    check_git_cli,
    check_mongo,
    check_neo4j,
    check_opencode_cli,
    check_redis,
    collect_models_to_probe,
    gather_deep_health,
    probe_openrouter_models,
)

# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeProcess:
    """Minimal stand-in for ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover — only used on timeout path
        pass


def _fake_subprocess_factory(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    raise_exc: BaseException | None = None,
) -> Any:
    async def _factory(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        if raise_exc is not None:
            raise raise_exc
        return _FakeProcess(returncode=returncode, stdout=stdout, stderr=stderr)

    return _factory


# ── git_cli ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_git_cli_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(stdout=b"git version 2.47.3\n"),
    )
    result = await check_git_cli()
    assert result == {"status": "ok", "version": "2.47.3"}


@pytest.mark.asyncio
async def test_check_git_cli_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(raise_exc=FileNotFoundError("git")),
    )
    result = await check_git_cli()
    assert result["status"] == "fail"
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_check_git_cli_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(returncode=128, stderr=b"fatal: oops\n"),
    )
    result = await check_git_cli()
    assert result["status"] == "fail"
    assert "fatal: oops" in result["error"]


# ── opencode_cli ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_opencode_cli_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(stdout=b"opencode 1.14.39\n"),
    )
    result = await check_opencode_cli()
    assert result == {"status": "ok", "version": "1.14.39"}


@pytest.mark.asyncio
async def test_check_opencode_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(raise_exc=FileNotFoundError("opencode")),
    )
    result = await check_opencode_cli()
    assert result["status"] == "fail"
    assert "not found" in result["error"]


# ── redis ────────────────────────────────────────────────────────────────


class _FakeRedisOk:
    @classmethod
    def from_url(cls, *_args: Any, **_kwargs: Any) -> _FakeRedisOk:
        return cls()

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeRedisFail:
    @classmethod
    def from_url(cls, *_args: Any, **_kwargs: Any) -> _FakeRedisFail:
        return cls()

    async def ping(self) -> bool:
        raise ConnectionError("nope")

    async def aclose(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_check_redis_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisOk)
    result = await check_redis("redis://fake:6379")
    assert result["status"] == "ok"
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_check_redis_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisFail)
    result = await check_redis("redis://fake:6379")
    assert result["status"] == "fail"
    assert "ConnectionError" in result["error"]


# ── mongo ────────────────────────────────────────────────────────────────


class _FakeMongoAdminOk:
    async def command(self, _cmd: str) -> dict[str, int]:
        return {"ok": 1}


class _FakeMongoOk:
    admin = _FakeMongoAdminOk()


class _FakeMongoAdminFail:
    async def command(self, _cmd: str) -> dict[str, int]:
        raise ConnectionError("no mongo")


class _FakeMongoFail:
    admin = _FakeMongoAdminFail()


@pytest.mark.asyncio
async def test_check_mongo_ok() -> None:
    result = await check_mongo(_FakeMongoOk())
    assert result["status"] == "ok"
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_check_mongo_fail() -> None:
    result = await check_mongo(_FakeMongoFail())
    assert result["status"] == "fail"
    assert "ConnectionError" in result["error"]


@pytest.mark.asyncio
async def test_check_mongo_none_client() -> None:
    result = await check_mongo(None)
    assert result["status"] == "fail"
    assert "not configured" in result["error"]


# ── neo4j ────────────────────────────────────────────────────────────────


class _FakeNeo4jResultOk:
    async def consume(self) -> None:
        return None


class _FakeNeo4jSessionOk:
    async def __aenter__(self) -> _FakeNeo4jSessionOk:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def run(self, _query: str) -> _FakeNeo4jResultOk:
        return _FakeNeo4jResultOk()


class _FakeNeo4jDriverOk:
    def session(self) -> _FakeNeo4jSessionOk:
        return _FakeNeo4jSessionOk()


class _FakeNeo4jSessionFail:
    async def __aenter__(self) -> _FakeNeo4jSessionFail:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def run(self, _query: str) -> Any:
        raise ConnectionError("graph down")


class _FakeNeo4jDriverFail:
    def session(self) -> _FakeNeo4jSessionFail:
        return _FakeNeo4jSessionFail()


@pytest.mark.asyncio
async def test_check_neo4j_ok() -> None:
    result = await check_neo4j(_FakeNeo4jDriverOk())
    assert result["status"] == "ok"
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_check_neo4j_fail() -> None:
    result = await check_neo4j(_FakeNeo4jDriverFail())
    assert result["status"] == "fail"
    assert "ConnectionError" in result["error"]


@pytest.mark.asyncio
async def test_check_neo4j_none_driver() -> None:
    result = await check_neo4j(None)
    assert result["status"] == "fail"


# ── probe_openrouter_models ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_openrouter_models_skipped_when_opencode_unavailable() -> None:
    _reset_cache_for_tests()
    result = await probe_openrouter_models(
        ["openrouter/google/gemini-2.5-pro"],
        opencode_available=False,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "opencode_cli unavailable"
    assert "openrouter/google/gemini-2.5-pro" in result["results"]


@pytest.mark.asyncio
async def test_probe_openrouter_models_no_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(
            returncode=1,
            stderr=b"Error: No endpoints found for model gemini-3-pro-preview\n",
        ),
    )
    result = await probe_openrouter_models(
        ["openrouter/google/gemini-3-pro-preview"],
        timeout_seconds=1.0,
    )
    assert result["results"]["openrouter/google/gemini-3-pro-preview"] == "fail: No endpoints found"
    assert result["status"] == "fail"
    assert result["probed_at"] is not None


@pytest.mark.asyncio
async def test_probe_openrouter_models_insufficient_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(
            returncode=1,
            stderr=b"Insufficient credits to complete this request\n",
        ),
    )
    result = await probe_openrouter_models(["openrouter/x/y"], timeout_seconds=1.0)
    assert result["results"]["openrouter/x/y"] == "fail: insufficient credits"


@pytest.mark.asyncio
async def test_probe_openrouter_models_other_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(
            returncode=2,
            stderr=b"context deadline exceeded\nsecondary line\n",
        ),
    )
    result = await probe_openrouter_models(["openrouter/x/y"], timeout_seconds=1.0)
    assert result["results"]["openrouter/x/y"] == "fail: context deadline exceeded"


@pytest.mark.asyncio
async def test_probe_openrouter_models_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(returncode=0, stdout=b"pong\n"),
    )
    result = await probe_openrouter_models(
        ["openrouter/google/gemini-2.5-pro"],
        timeout_seconds=1.0,
    )
    assert result["status"] == "ok"
    assert result["results"]["openrouter/google/gemini-2.5-pro"] == "ok"


@pytest.mark.asyncio
async def test_probe_openrouter_models_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cache_for_tests()
    call_counter = {"n": 0}

    async def _factory(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        call_counter["n"] += 1
        return _FakeProcess(returncode=0, stdout=b"pong\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _factory)

    first = await probe_openrouter_models(["openrouter/m1"], timeout_seconds=1.0)
    assert first["status"] == "ok"
    assert call_counter["n"] == 1

    # Second call within the TTL must not re-spawn the subprocess.
    second = await probe_openrouter_models(["openrouter/m1"], timeout_seconds=1.0)
    assert second["status"] == "ok"
    assert call_counter["n"] == 1
    # probed_at survives across calls (same cache entry).
    assert first["probed_at"] == second["probed_at"]


# ── collect_models_to_probe ──────────────────────────────────────────────


class _FakeOpenCodeLocal:
    def __init__(self) -> None:
        self.model = "openrouter/google/gemini-2.5-pro"
        self.fix_model = "openrouter/google/gemini-2.5-pro"
        self.review_models = {
            "trivial": "openrouter/google/gemini-2.5-flash-lite",
            "complex": "openrouter/google/gemini-2.5-pro",
        }
        self.fix_models = {
            "complex": "openrouter/anthropic/claude-sonnet-4.5",
        }


class _FakeFeatureModel:
    def __init__(self, model: str | None) -> None:
        self.model = model
        self.max_tokens = None


class _FakeLLM:
    def __init__(
        self,
        provider: str = "openrouter",
        feature_models: dict[str, _FakeFeatureModel] | None = None,
    ) -> None:
        self.provider = provider
        self.feature_models = feature_models or {}


def test_collect_models_dedupes_and_includes_classifier() -> None:
    models = collect_models_to_probe(
        opencode_local=_FakeOpenCodeLocal(),
        llm=_FakeLLM(),
    )
    # Pro appears multiple times but is deduplicated.
    assert models.count("openrouter/google/gemini-2.5-pro") == 1
    # Classifier resolves to provider default for openrouter.
    assert "openrouter/google/gemini-2.5-flash-lite" in models
    # Sonnet from fix_models is present.
    assert "openrouter/anthropic/claude-sonnet-4.5" in models


def test_collect_models_uses_operator_override() -> None:
    llm = _FakeLLM(
        feature_models={"complexity_classifier": _FakeFeatureModel("openrouter/custom/model")},
    )
    models = collect_models_to_probe(opencode_local=_FakeOpenCodeLocal(), llm=llm)
    assert "openrouter/custom/model" in models


# ── gather_deep_health rollup ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_deep_health_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(returncode=0, stdout=b"git version 2.47.3\n"),
    )

    # Stub out the model probe entirely — covered above.
    async def _fake_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "probed_at": "2026-05-06T00:00:00+00:00", "results": {}}

    monkeypatch.setattr(health_module, "probe_openrouter_models", _fake_probe)

    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisOk)

    result = await gather_deep_health(
        redis_url="redis://fake:6379",
        mongo_client=_FakeMongoOk(),
        neo4j_driver=_FakeNeo4jDriverOk(),
        models_to_probe=["openrouter/m1"],
        version="0.28.4",
    )
    assert result["status"] == "ok"
    assert result["version"] == "0.28.4"
    assert set(result["checks"].keys()) == {
        "git_cli",
        "opencode_cli",
        "redis",
        "mongo",
        "neo4j",
        "openrouter_models",
    }


@pytest.mark.asyncio
async def test_gather_deep_health_degraded_on_model_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(returncode=0, stdout=b"git version 2.47.3\n"),
    )

    async def _fake_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "fail",
            "probed_at": "2026-05-06T00:00:00+00:00",
            "results": {"openrouter/m1": "fail: No endpoints found"},
        }

    monkeypatch.setattr(health_module, "probe_openrouter_models", _fake_probe)

    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisOk)

    result = await gather_deep_health(
        redis_url="redis://fake:6379",
        mongo_client=_FakeMongoOk(),
        neo4j_driver=_FakeNeo4jDriverOk(),
        models_to_probe=["openrouter/m1"],
    )
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_gather_deep_health_fail_on_redis_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_subprocess_factory(returncode=0, stdout=b"git version 2.47.3\n"),
    )

    async def _fake_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "probed_at": None, "results": {}}

    monkeypatch.setattr(health_module, "probe_openrouter_models", _fake_probe)

    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisFail)

    result = await gather_deep_health(
        redis_url="redis://fake:6379",
        mongo_client=_FakeMongoOk(),
        neo4j_driver=_FakeNeo4jDriverOk(),
        models_to_probe=[],
    )
    assert result["status"] == "fail"
    assert result["checks"]["redis"]["status"] == "fail"


@pytest.mark.asyncio
async def test_gather_deep_health_skips_model_probe_when_opencode_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_cache_for_tests()

    # First subprocess call (git) succeeds; second (opencode) raises FileNotFoundError.
    call_log: list[tuple[str, ...]] = []

    async def _factory(*args: Any, **_kwargs: Any) -> _FakeProcess:
        cmd = tuple(str(a) for a in args)
        call_log.append(cmd)
        if cmd and cmd[0] == "git":
            return _FakeProcess(returncode=0, stdout=b"git version 2.47.3\n")
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _factory)

    import redis.asyncio as redis_async

    monkeypatch.setattr(redis_async, "Redis", _FakeRedisOk)

    result = await gather_deep_health(
        redis_url="redis://fake:6379",
        mongo_client=_FakeMongoOk(),
        neo4j_driver=_FakeNeo4jDriverOk(),
        models_to_probe=["openrouter/m1", "openrouter/m2"],
    )
    # opencode missing → core fail → top-level fail
    assert result["status"] == "fail"
    assert result["checks"]["opencode_cli"]["status"] == "fail"
    # Model probe should be skipped (no per-model subprocess attempts).
    assert result["checks"]["openrouter_models"]["status"] == "skipped"
    # Verify we never spawned an "opencode run ping" subprocess.
    assert all(cmd[0] != "opencode" or "run" not in cmd for cmd in call_log)
