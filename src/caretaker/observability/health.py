"""Deep dependency health checks for the ``/health/deep`` endpoint.

Probes every external dependency caretaker needs at runtime:

* ``git`` and ``opencode`` CLI binaries (must be on PATH inside the pod)
* Redis, MongoDB, Neo4j connectivity
* OpenRouter availability of every model the PR-reviewer plans to use

The expensive piece is the OpenRouter probe: each model costs one round-trip
of ``opencode run "ping"``, so we cache results for an hour to bound both
wall-time and dollars when an operator (or, defensively, a kubelet) hits
``/health/deep`` repeatedly.

The other five checks are deliberately uncached — they're sub-millisecond
pings and we always want them to reflect live state.

Catches the v0.28.x-class bugs (``git`` missing from the production image,
``gemini-3-pro-preview`` absent from OpenRouter's registry) at deploy time
rather than letting them surface as silent reviews on real PRs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

MODEL_PROBE_TTL_SECONDS: float = 3600.0
"""Cache TTL for OpenRouter model probes (1 hour)."""

_GIT_VERSION_RE = re.compile(r"git\s+version\s+(\S+)", re.IGNORECASE)
_OPENCODE_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\S*)")


# ── Cache ────────────────────────────────────────────────────────────────


@dataclass
class ModelProbeResult:
    """Cached outcome of probing a single OpenRouter model."""

    status: str
    """Either ``"ok"`` or ``"fail: <reason>"``."""

    probed_at: float
    """``time.time()`` at which the probe completed (used for TTL)."""

    probed_at_iso: str
    """ISO-8601 timestamp of ``probed_at`` for human-readable surfacing."""


_model_probe_cache: dict[str, ModelProbeResult] = {}


def _reset_cache_for_tests() -> None:
    """Test helper: drop all cached model probes."""
    _model_probe_cache.clear()


# ── Helper: subprocess with timeout ──────────────────────────────────────


async def _run_cli(
    *args: str,
    timeout_seconds: float,
) -> tuple[int, str, str]:
    """Run a CLI command and return ``(returncode, stdout, stderr)``.

    Raises ``asyncio.TimeoutError`` on wall-time overrun, ``FileNotFoundError``
    when the binary doesn't exist on PATH, and propagates any other OSError
    so callers can map it into a structured failure record.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        # Make sure we don't leak the child process on timeout.
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


# ── CLI checks ───────────────────────────────────────────────────────────


async def check_git_cli(timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Probe ``git --version``. Returns ``{"status", "version"}`` or fail."""
    try:
        rc, stdout, stderr = await _run_cli(
            "git",
            "--version",
            timeout_seconds=timeout_seconds,
        )
    except FileNotFoundError:
        return {"status": "fail", "error": "git binary not found on PATH"}
    except TimeoutError:
        return {"status": "fail", "error": f"git --version timed out after {timeout_seconds}s"}
    except OSError as exc:
        return {"status": "fail", "error": f"git --version failed: {exc}"}

    if rc != 0:
        msg = (stderr.strip() or stdout.strip() or f"exit {rc}").splitlines()[0]
        return {"status": "fail", "error": msg}

    match = _GIT_VERSION_RE.search(stdout)
    version = match.group(1) if match else stdout.strip().splitlines()[0]
    return {"status": "ok", "version": version}


async def check_opencode_cli(timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Probe ``opencode --version``. Returns ``{"status", "version"}`` or fail."""
    try:
        rc, stdout, stderr = await _run_cli(
            "opencode",
            "--version",
            timeout_seconds=timeout_seconds,
        )
    except FileNotFoundError:
        return {"status": "fail", "error": "opencode binary not found on PATH"}
    except TimeoutError:
        return {
            "status": "fail",
            "error": f"opencode --version timed out after {timeout_seconds}s",
        }
    except OSError as exc:
        return {"status": "fail", "error": f"opencode --version failed: {exc}"}

    if rc != 0:
        msg = (stderr.strip() or stdout.strip() or f"exit {rc}").splitlines()[0]
        return {"status": "fail", "error": msg}

    text = (stdout or stderr).strip()
    match = _OPENCODE_VERSION_RE.search(text)
    version = match.group(1) if match else (text.splitlines()[0] if text else "")
    return {"status": "ok", "version": version}


# ── Datastore checks ─────────────────────────────────────────────────────


async def check_redis(redis_url: str, timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Ping Redis and report latency."""
    try:
        from redis.asyncio import Redis as AsyncRedis
    except ImportError:
        return {"status": "fail", "error": "redis package not installed"}

    client = AsyncRedis.from_url(
        redis_url,
        socket_timeout=timeout_seconds,
        socket_connect_timeout=timeout_seconds,
    )
    start = time.perf_counter()
    try:
        await asyncio.wait_for(client.ping(), timeout=timeout_seconds)
    except TimeoutError:
        return {"status": "fail", "error": f"redis ping timed out after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001 — surface redis.exceptions.* + connect errors
        return {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        # Prefer ``aclose`` (redis>=5.0.1) but fall back to ``close`` for
        # older clients. The typeshed stubs only expose ``close`` so we
        # use ``getattr`` to keep mypy happy without a ``cast``.
        with suppress(Exception):
            closer = getattr(client, "aclose", client.close)
            await closer()
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {"status": "ok", "latency_ms": latency_ms}


async def check_mongo(mongo_client: Any, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Ping MongoDB via ``admin.command("ping")`` and report latency."""
    if mongo_client is None:
        return {"status": "fail", "error": "mongo client not configured"}
    start = time.perf_counter()
    try:
        await asyncio.wait_for(
            mongo_client.admin.command("ping"),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return {"status": "fail", "error": f"mongo ping timed out after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001 — pymongo + motor raise heterogeneous errors
        return {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {"status": "ok", "latency_ms": latency_ms}


async def check_neo4j(neo4j_driver: Any, timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Run a trivial Cypher query on Neo4j and report latency."""
    if neo4j_driver is None:
        return {"status": "fail", "error": "neo4j driver not configured"}
    start = time.perf_counter()
    try:
        async with neo4j_driver.session() as session:
            result = await asyncio.wait_for(
                session.run("RETURN 1 AS ok"),
                timeout=timeout_seconds,
            )
            await asyncio.wait_for(result.consume(), timeout=timeout_seconds)
    except TimeoutError:
        return {"status": "fail", "error": f"neo4j query timed out after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001 — neo4j.exceptions.* surface
        return {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {"status": "ok", "latency_ms": latency_ms}


# ── OpenRouter model probe ──────────────────────────────────────────────


async def _probe_single_model(
    model: str,
    cli_path: str,
    timeout_seconds: float,
) -> str:
    """Run ``opencode run "ping" --model <model>`` once and classify the result."""
    try:
        rc, _stdout, stderr = await _run_cli(
            cli_path,
            "run",
            "ping",
            "--model",
            model,
            timeout_seconds=timeout_seconds,
        )
    except FileNotFoundError:
        return "fail: opencode binary not found on PATH"
    except TimeoutError:
        return f"fail: timeout after {timeout_seconds}s"
    except OSError as exc:
        return f"fail: {exc}"

    if rc == 0:
        return "ok"

    # Pattern-match the most common OpenRouter failure modes so operators
    # can grep dashboards for "No endpoints found" and find every affected
    # deploy at once.
    if "No endpoints found" in stderr:
        return "fail: No endpoints found"
    if "Insufficient credits" in stderr or "insufficient credits" in stderr:
        return "fail: insufficient credits"
    first_line = next(
        (line for line in stderr.splitlines() if line.strip()),
        "",
    )
    return f"fail: {first_line}" if first_line else f"fail: exit {rc}"


async def probe_openrouter_models(
    models: Iterable[str],
    cli_path: str = "opencode",
    timeout_seconds: float = 30.0,
    *,
    opencode_available: bool = True,
) -> dict[str, Any]:
    """Probe every model concurrently, with a 1-hour result cache.

    When ``opencode_available`` is False the entire check short-circuits to
    a ``skipped`` record — there's no point firing N subprocess calls that
    will all fail with the same "binary missing" error.
    """
    model_list = list(dict.fromkeys(models))  # de-dupe, preserve order
    if not model_list:
        return {
            "status": "skipped",
            "reason": "no models configured",
            "results": {},
        }

    if not opencode_available:
        return {
            "status": "skipped",
            "reason": "opencode_cli unavailable",
            "results": dict.fromkeys(model_list, "skipped: opencode_cli unavailable"),
        }

    now = time.time()
    to_probe: list[str] = []
    for model in model_list:
        cached = _model_probe_cache.get(model)
        if cached is None or (now - cached.probed_at) >= MODEL_PROBE_TTL_SECONDS:
            to_probe.append(model)

    if to_probe:
        coros = [_probe_single_model(m, cli_path, timeout_seconds) for m in to_probe]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        finished_at = time.time()
        finished_iso = datetime.fromtimestamp(finished_at, tz=UTC).isoformat()
        for model, outcome in zip(to_probe, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                status = f"fail: {type(outcome).__name__}: {outcome}"
            else:
                status = outcome
            _model_probe_cache[model] = ModelProbeResult(
                status=status,
                probed_at=finished_at,
                probed_at_iso=finished_iso,
            )

    results: dict[str, str] = {}
    oldest_probed_iso: str | None = None
    oldest_probed_at: float | None = None
    any_fail = False
    for model in model_list:
        entry = _model_probe_cache.get(model)
        if entry is None:
            # Should not happen — defensive.
            results[model] = "fail: probe missing from cache"
            any_fail = True
            continue
        results[model] = entry.status
        if entry.status != "ok":
            any_fail = True
        if oldest_probed_at is None or entry.probed_at < oldest_probed_at:
            oldest_probed_at = entry.probed_at
            oldest_probed_iso = entry.probed_at_iso

    return {
        "status": "fail" if any_fail else "ok",
        "probed_at": oldest_probed_iso,
        "results": results,
    }


# ── Model-list extraction ────────────────────────────────────────────────


def collect_models_to_probe(
    *,
    opencode_local: Any,
    llm: Any,
) -> list[str]:
    """Return the deduplicated list of model IDs to probe.

    Pulls from:
      * ``opencode_local.model``, ``opencode_local.fix_model``
      * every value in ``opencode_local.review_models`` and ``fix_models``
      * the ``complexity_classifier`` feature model — operator override
        (``llm.feature_models["complexity_classifier"].model``) wins, otherwise
        the provider default from ``DEFAULT_FEATURE_MODELS_BY_PROVIDER``,
        otherwise the legacy ``DEFAULT_FEATURE_MODELS`` entry.
    """
    seen: list[str] = []

    def _add(value: str | None) -> None:
        if value and value not in seen:
            seen.append(value)

    _add(getattr(opencode_local, "model", None))
    _add(getattr(opencode_local, "fix_model", None))
    for value in (getattr(opencode_local, "review_models", {}) or {}).values():
        _add(value)
    for value in (getattr(opencode_local, "fix_models", {}) or {}).values():
        _add(value)

    # complexity_classifier resolution mirrors ClaudeClient._resolve_feature.
    from caretaker.config import (
        DEFAULT_FEATURE_MODELS,
        DEFAULT_FEATURE_MODELS_BY_PROVIDER,
    )

    classifier_model: str | None = None
    feature_models = getattr(llm, "feature_models", {}) or {}
    override = feature_models.get("complexity_classifier")
    if override is not None and getattr(override, "model", None):
        classifier_model = override.model
    if classifier_model is None:
        provider = getattr(llm, "provider", "")
        by_provider = DEFAULT_FEATURE_MODELS_BY_PROVIDER.get(provider, {})
        base = by_provider.get("complexity_classifier") or DEFAULT_FEATURE_MODELS.get(
            "complexity_classifier", {}
        )
        candidate = base.get("model") if isinstance(base, dict) else None
        if isinstance(candidate, str):
            classifier_model = candidate
    _add(classifier_model)

    return seen


# ── Aggregator ───────────────────────────────────────────────────────────


_CORE_CHECK_KEYS: tuple[str, ...] = ("git_cli", "opencode_cli", "redis", "mongo", "neo4j")


def _result_from(value: Any, fallback_error: str) -> dict[str, Any]:
    """Map a ``gather`` outcome to a structured check record."""
    if isinstance(value, BaseException):
        return {"status": "fail", "error": f"{type(value).__name__}: {value}"}
    if isinstance(value, dict):
        return value
    return {"status": "fail", "error": fallback_error}


async def gather_deep_health(
    *,
    redis_url: str,
    mongo_client: Any,
    neo4j_driver: Any,
    models_to_probe: list[str],
    opencode_cli_path: str = "opencode",
    version: str = "",
) -> dict[str, Any]:
    """Run all six checks concurrently and return the combined verdict."""
    core_results: list[Any] = await asyncio.gather(
        check_git_cli(),
        check_opencode_cli(),
        check_redis(redis_url),
        check_mongo(mongo_client),
        check_neo4j(neo4j_driver),
        return_exceptions=True,
    )

    git_check = _result_from(core_results[0], "git check raised")
    opencode_check = _result_from(core_results[1], "opencode check raised")
    redis_check = _result_from(core_results[2], "redis check raised")
    mongo_check = _result_from(core_results[3], "mongo check raised")
    neo4j_check = _result_from(core_results[4], "neo4j check raised")

    # Model probe is sequenced AFTER opencode_cli so we can short-circuit
    # the N subprocess calls when the binary itself is missing.
    opencode_available = opencode_check.get("status") == "ok"
    try:
        models_check = await probe_openrouter_models(
            models_to_probe,
            cli_path=opencode_cli_path,
            opencode_available=opencode_available,
        )
    except Exception as exc:  # noqa: BLE001 — defensive; never let aggregator raise
        logger.exception("probe_openrouter_models raised unexpectedly")
        models_check = {
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "results": {},
        }

    checks: dict[str, dict[str, Any]] = {
        "git_cli": git_check,
        "opencode_cli": opencode_check,
        "redis": redis_check,
        "mongo": mongo_check,
        "neo4j": neo4j_check,
        "openrouter_models": models_check,
    }

    # Rollup:
    #   any core check fail → top-level "fail" (HTTP 503 at the endpoint)
    #   any non-core "fail" or "skipped" (e.g. model probe) → "degraded"
    #   else → "ok"
    core_failed = any(checks[k].get("status") != "ok" for k in _CORE_CHECK_KEYS)
    models_status = checks["openrouter_models"].get("status")

    if core_failed:
        rollup = "fail"
    elif models_status in ("fail", "degraded", "skipped"):
        rollup = "degraded"
    else:
        rollup = "ok"

    return {
        "status": rollup,
        "version": version,
        "checks": checks,
    }
