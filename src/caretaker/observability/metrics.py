"""Prometheus metrics instrumentation — paved-path observability SKILL.

Complement to :mod:`caretaker.observability.otel` (traces). This module
emits the RED-floor HTTP metrics, the `http_client_*` counterpart for
outbound GitHub calls, the `db_client_*` family for every persistent
store (Redis, Mongo, Neo4j), `worker_jobs_total` / `worker_job_duration_seconds`
for agent dispatch, and a `caretaker_rate_limit_cooldown_seconds` gauge
so the GitHub rate-limit backoff is observable without reading logs.

Design constraints (SKILL §1 non-negotiables):

* ``/metrics`` is served on a **separate port** (default ``:9090``) named
  ``metrics``. Never on the user-traffic port.
* Default path is ``/metrics``. No auth — cluster mesh mTLS handles it.
* Every metric name follows OTel semantic conventions.
* Histograms use the curated §3 latency buckets; we never ship default
  bucket edges to production.
* Label values are bounded enums; no user id, email, trace id, or raw
  URL path is ever a label (§4). Route labels are templated.
* Trace correlation travels via the exemplar API, not as a label.

Usage
-----

    from fastapi import FastAPI
    from caretaker.observability.metrics import init_metrics

    app = FastAPI()
    # Called inside the FastAPI lifespan handler (next to init_tracing).
    init_metrics(app, service="caretaker-mcp")
"""

from __future__ import annotations

import asyncio
import functools
import importlib.metadata
import logging
import os
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypeVar

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# ── Curated histogram buckets (SKILL §3) ─────────────────────────────

# Latency buckets for request durations — 11 edges, keeps series under
# the cardinality cap even when multiplied by the label fan-out.
LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

# ── Metrics registry ──────────────────────────────────────────────────
#
# A dedicated :class:`CollectorRegistry` so tests can reset the module
# state without touching the default global registry (which carries the
# `process_*` + `python_*` collectors we want to keep for the live
# ``/metrics`` output).

REGISTRY = CollectorRegistry()


# ── HTTP server (RED floor) ──────────────────────────────────────────
#
# ``prometheus-fastapi-instrumentator`` emits the Starlette-side HTTP
# metrics under the same names, but we declare them here so tests have
# a stable import path and so the bucket policy is one-line reviewable.

HTTP_SERVER_REQUESTS_TOTAL = Counter(
    "http_server_requests_total",
    "Total HTTP requests served (RED floor counter).",
    ["service", "http_method", "http_route", "http_status_code"],
    registry=REGISTRY,
)

HTTP_SERVER_REQUEST_DURATION_SECONDS = Histogram(
    "http_server_request_duration_seconds",
    "HTTP server request latency in seconds (RED floor histogram).",
    ["service", "http_method", "http_route", "http_status_code"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

# ── Outbound HTTP client (GitHub) ─────────────────────────────────────

HTTP_CLIENT_REQUESTS_TOTAL = Counter(
    "http_client_requests_total",
    "Total outbound HTTP requests from caretaker to external services.",
    ["service", "peer_service", "http_method", "http_status_code"],
    registry=REGISTRY,
)

HTTP_CLIENT_REQUEST_DURATION_SECONDS = Histogram(
    "http_client_request_duration_seconds",
    "Outbound HTTP request latency in seconds.",
    ["service", "peer_service", "http_method", "http_status_code"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

# ── DB client (Redis / Mongo / Neo4j) ─────────────────────────────────

DB_CLIENT_OPERATIONS_TOTAL = Counter(
    "db_client_operations_total",
    "Total database operations issued by caretaker.",
    ["service", "db_system", "db_operation", "outcome"],
    registry=REGISTRY,
)

DB_CLIENT_OPERATION_DURATION_SECONDS = Histogram(
    "db_client_operation_duration_seconds",
    "Database operation latency in seconds.",
    ["service", "db_system", "db_operation"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

# ── Worker / job metrics ──────────────────────────────────────────────

WORKER_JOBS_TOTAL = Counter(
    "worker_jobs_total",
    "Total background/agent jobs executed.",
    ["service", "job", "outcome"],
    registry=REGISTRY,
)

WORKER_JOB_DURATION_SECONDS = Histogram(
    "worker_job_duration_seconds",
    "Background/agent job latency in seconds.",
    ["service", "job", "outcome"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

WORKER_QUEUE_DEPTH = Gauge(
    "worker_queue_depth",
    "Depth of the pending work queue for a named worker pool.",
    ["service", "queue"],
    registry=REGISTRY,
)

# ── GitHub webhook dispatch (Phase 2 App) ─────────────────────────────
#
# Counts every webhook delivery that reaches the dispatcher, labelled by
# the resolved event type, the dispatcher mode (off/shadow/active), and
# the terminal outcome. Kept separate from ``worker_jobs_total`` because
# one webhook can fan out to N agent invocations — we want cardinality
# for "how many deliveries arrived" vs. "how many agent jobs ran".

WEBHOOK_EVENTS_TOTAL = Counter(
    "caretaker_webhook_events_total",
    "Total GitHub webhook deliveries handled by the dispatcher.",
    ["service", "event", "mode", "outcome"],
    registry=REGISTRY,
)

# ── Domain errors ────────────────────────────────────────────────────

CARETAKER_ERRORS_TOTAL = Counter(
    "caretaker_errors_total",
    "Classified caretaker errors.",
    ["service", "kind"],
    registry=REGISTRY,
)

# Incremented when an orchestrator run completes with only transient agent
# errors and has been allowed to exit 0 (soft-fail). The ``category`` label
# is a bounded enum (``transient`` today; future categories reserved so the
# series cardinality is capped at one digit).
ORCHESTRATOR_SOFT_FAIL_TOTAL = Counter(
    "caretaker_orchestrator_soft_fail_total",
    "Orchestrator runs that soft-failed (all agent errors transient, exit 0).",
    ["service", "category"],
    registry=REGISTRY,
)

# ── Rate limit visibility ────────────────────────────────────────────

RATE_LIMIT_COOLDOWN_SECONDS = Gauge(
    "caretaker_rate_limit_cooldown_seconds",
    "Seconds remaining in the GitHub rate-limit cooldown window.",
    ["service", "peer_service"],
    registry=REGISTRY,
)

RATE_LIMIT_REMAINING = Gauge(
    "caretaker_rate_limit_remaining",
    "Last observed X-RateLimit-Remaining value for a peer service.",
    ["service", "peer_service"],
    registry=REGISTRY,
)

# ── Token scope gap visibility ───────────────────────────────────────
#
# Counts 403 "Resource not accessible by integration" responses by the
# scope the token is missing. Bounded cardinality: the label values
# come from the curated map in
# :mod:`caretaker.github_client.scope_gap`, so the cardinality is
# governed by the length of that map plus one ``metadata: read``
# fallback.

GITHUB_SCOPE_GAP_TOTAL = Counter(
    "caretaker_github_scope_gap_total",
    "Total 403 responses caused by a missing GitHub token scope, by scope hint.",
    ["service", "scope"],
    registry=REGISTRY,
)

# ── Attribution telemetry (R&D workstream A2) ───────────────────────
#
# Answers "did caretaker actually save human toil this week?" The three
# counters below aggregate the per-PR / per-issue booleans stored on
# :class:`~caretaker.state.models.TrackedPR` /
# :class:`~caretaker.state.models.TrackedIssue` into rolling totals the
# admin dashboard can graph. Cardinality is bounded by a small fixed
# enum of ``outcome`` / ``reason`` labels and the set of repos caretaker
# runs in.
#
# The ``repo`` label is the ``owner/repo`` slug. We intentionally do not
# include per-PR numbers — that would blow up cardinality to something
# like one series per open PR, and Prometheus would start evicting
# series at scale. The per-PR state lives in the graph store instead.

CARETAKER_PR_OUTCOME_TOTAL = Counter(
    "caretaker_pr_outcome_total",
    "PR outcomes classified by attribution status (touched / merged / "
    "closed_unmerged / operator_rescued / abandoned).",
    ["service", "repo", "outcome"],
    registry=REGISTRY,
)

CARETAKER_ISSUE_OUTCOME_TOTAL = Counter(
    "caretaker_issue_outcome_total",
    "Issue outcomes classified by attribution status (triaged / "
    "closed_by_caretaker / closed_by_operator / stale_closed).",
    ["service", "repo", "outcome"],
    registry=REGISTRY,
)

CARETAKER_OPERATOR_INTERVENTION_TOTAL = Counter(
    "caretaker_operator_intervention_total",
    "Human interventions that superseded a caretaker action, by reason.",
    ["service", "repo", "reason"],
    registry=REGISTRY,
)

# Bounded enums for the ``outcome`` / ``reason`` labels. Re-exported so
# callers don't fat-finger label values (which would silently create new
# series and break the cardinality budget).
PR_OUTCOMES: frozenset[str] = frozenset(
    {"touched", "merged", "closed_unmerged", "operator_rescued", "abandoned"}
)
ISSUE_OUTCOMES: frozenset[str] = frozenset(
    {"triaged", "closed_by_caretaker", "closed_by_operator", "stale_closed"}
)
INTERVENTION_REASONS: frozenset[str] = frozenset(
    {"manual_merge", "manual_close", "label_changed", "force_push", "commit_added"}
)


# ── LLM prompt-cache telemetry ───────────────────────────────────────
#
# Emitted by :mod:`caretaker.llm.provider` on every completion.  Together
# they let Grafana compute an Anthropic prompt-cache hit ratio:
#
#     sum(rate(caretaker_llm_cache_read_tokens_total[5m]))
#       / sum(rate(caretaker_llm_cache_read_tokens_total[5m])
#             + rate(caretaker_llm_cache_creation_tokens_total[5m]))
#
# Labelled by ``provider`` (``anthropic`` / ``litellm``) and ``model`` so
# we can attribute hits per-provider and per-model.  Cardinality stays low:
# a handful of providers × tens of model ids.

LLM_CACHE_READ_TOKENS_TOTAL = Counter(
    "caretaker_llm_cache_read_tokens_total",
    "Input tokens served from Anthropic prompt cache (cache hits).",
    ["provider", "model"],
    registry=REGISTRY,
)

LLM_CACHE_CREATION_TOKENS_TOTAL = Counter(
    "caretaker_llm_cache_creation_tokens_total",
    "Input tokens written into the Anthropic prompt cache (cache misses).",
    ["provider", "model"],
    registry=REGISTRY,
)

# ── Self-heal fix ladder (Wave A3) ───────────────────────────────────
#
# The deterministic-first fix ladder emits one of these counters per
# rung execution so operators can watch "did we fix it with ruff or
# did it escalate?" without tailing workflow logs. Cardinality stays
# bounded: ``rung`` is a closed enum matching
# :data:`caretaker.self_heal_agent.fix_ladder.DEFAULT_RUNGS` plus any
# operator-supplied rung names (we trust the config validator to keep
# those small), and ``outcome`` is a 3-value enum below.

FIX_LADDER_OUTCOME_TOTAL = Counter(
    "caretaker_fix_ladder_outcome_total",
    "Self-heal fix-ladder rung outcomes (Wave A3).",
    ["repo", "rung", "outcome"],
    registry=REGISTRY,
)

# Incremented when the ladder gives up and falls through to the LLM
# escalation path. The ``error_sig_hash`` label is the raw 12-char
# signature — same value the storm cap keys on — so operators can
# correlate a recurring escalation with the underlying failure. The
# signature is already low-cardinality (hash space shared across
# thousands of repos with a handful of signatures each) so this
# doesn't blow up the series count.

FIX_LADDER_ESCALATION_TOTAL = Counter(
    "caretaker_fix_ladder_escalation_total",
    "Self-heal fix-ladder runs that escalated to the LLM path.",
    ["repo", "error_sig_hash"],
    registry=REGISTRY,
)


# ── Guardrails (Ch. 18) ──────────────────────────────────────────────
#
# Emitted by :mod:`caretaker.guardrails` on every sanitize, filter, and
# rollback event. Cardinality is bounded: ``source`` / ``target`` are
# closed enums defined in the guardrails package; ``modification_type``
# and ``reason`` are short bounded strings so the full series count
# stays in the low hundreds even at maximum fan-out.

GUARDRAIL_SANITIZE_TOTAL = Counter(
    "caretaker_guardrail_sanitize_total",
    "External inputs passed through sanitize_input, by source and modification.",
    ["service", "source", "modification_type"],
    registry=REGISTRY,
)

GUARDRAIL_FILTER_BLOCKED_TOTAL = Counter(
    "caretaker_guardrail_filter_blocked_total",
    "Outbound writes blocked or modified by filter_output, by target and reason.",
    ["service", "target", "reason"],
    registry=REGISTRY,
)

GUARDRAIL_ROLLBACK_FIRED_TOTAL = Counter(
    "caretaker_guardrail_rollback_fired_total",
    "checkpoint_and_rollback invocations whose rollback path actually fired.",
    ["service", "repo", "reason"],
    registry=REGISTRY,
)


def record_fix_ladder_outcome(repo: str, rung: str, outcome: str) -> None:
    """Module-level sink matching the runner's ``metrics_sink`` callable."""
    FIX_LADDER_OUTCOME_TOTAL.labels(repo=repo or "unknown", rung=rung, outcome=outcome).inc()


def record_fix_ladder_escalation(repo: str, error_sig_hash: str) -> None:
    """Module-level sink matching the runner's ``escalation_metrics_sink``."""
    FIX_LADDER_ESCALATION_TOTAL.labels(
        repo=repo or "unknown",
        error_sig_hash=error_sig_hash or "unknown",
    ).inc()


# ── Build / version metadata ─────────────────────────────────────────

APP_INFO = Gauge(
    "app_info",
    "Caretaker build / version metadata (always 1).",
    ["service", "version", "commit"],
    registry=REGISTRY,
)


# Module-level state populated by :func:`init_metrics`. The service
# label is the *only* piece of process-wide context that every instr
# hook needs, so threading it through every call site would be noisy.
_SERVICE_LABEL: str = "caretaker"
_METRICS_INITIALISED = False
_METRICS_SERVER_TASK: asyncio.Task[None] | None = None


def get_service_label() -> str:
    """Return the service label set by :func:`init_metrics`."""
    return _SERVICE_LABEL


# ── FastAPI wiring ───────────────────────────────────────────────────


def _templated_route(request: Any) -> str:
    """Return a templated route label for ``request`` (no raw paths).

    Falls back to ``"<unmatched>"`` when FastAPI did not match a route
    (404s) so we never leak high-cardinality raw paths.
    """
    scope = getattr(request, "scope", None) or {}
    route = scope.get("route") if isinstance(scope, dict) else None
    path = getattr(route, "path", None) if route is not None else None
    if isinstance(path, str) and path:
        return path
    return "<unmatched>"


def _bucket_status(code: int) -> str:
    """Return the full HTTP status code as a string label.

    Status codes are bounded to ~60 values, well within the cardinality
    budget. We keep the full code so ``4xx`` drilldowns stay precise.
    """
    return str(code)


def init_metrics(app: FastAPI, service: str = "caretaker") -> None:
    """Instrument a FastAPI app with the RED-floor metrics.

    Called from the app's lifespan handler. Idempotent — a second call
    with a different service name updates the label but does not
    re-register collectors.

    This function deliberately does *not* mount ``/metrics`` on the user
    app. A separate ASGI server is started by
    :func:`start_metrics_server` (called from the lifespan) on a
    different port (default ``9090``) so scraping never contends with
    user traffic (SKILL §1).
    """
    global _SERVICE_LABEL, _METRICS_INITIALISED  # noqa: PLW0603 - process singleton

    _SERVICE_LABEL = service

    # Publish build metadata once per process. Using a set-valued gauge
    # so a hot reload doesn't leave a stale series hanging.
    try:
        version = importlib.metadata.version("caretaker-github")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        version = "0.0.0"
    commit = os.environ.get("CARETAKER_GIT_SHA", "unknown")
    APP_INFO.labels(service=service, version=version, commit=commit).set(1)

    # Middleware registration is per-app (each FastAPI instance has its
    # own middleware stack). The module-level ``_METRICS_INITIALISED``
    # flag only guards process-wide side-effects (e.g. a future metrics
    # exporter singleton) — adding the same middleware twice to the
    # *same* app would double-count, so callers must call ``init_metrics``
    # exactly once per app.

    # HTTP server RED metrics. We use our own lightweight middleware
    # (not the upstream instrumentator) because:
    #   1. it lets us honour the templated-route contract strictly, and
    #   2. we want metrics + labels to live in one registry so tests
    #      can assert cardinality without sampling both.
    @app.middleware("http")
    async def _record_http_metrics(request: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = getattr(response, "status_code", 500)
            return response
        except Exception:
            status_code = 500
            CARETAKER_ERRORS_TOTAL.labels(service=service, kind="internal").inc()
            raise
        finally:
            duration = time.perf_counter() - start
            method = getattr(request, "method", "GET") or "GET"
            route = _templated_route(request)
            code_label = _bucket_status(status_code)
            HTTP_SERVER_REQUESTS_TOTAL.labels(
                service=service,
                http_method=method,
                http_route=route,
                http_status_code=code_label,
            ).inc()
            HTTP_SERVER_REQUEST_DURATION_SECONDS.labels(
                service=service,
                http_method=method,
                http_route=route,
                http_status_code=code_label,
            ).observe(duration)

    _METRICS_INITIALISED = True
    logger.info("Prometheus metrics initialised (service=%s)", service)


# ── Metrics ASGI app + separate-port server ──────────────────────────


async def _metrics_asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Minimal ASGI app serving the caretaker :data:`REGISTRY` on ``/metrics``.

    We hand-roll this instead of using :func:`prometheus_client.make_asgi_app`
    because that helper only exposes the default registry.
    """
    if scope["type"] != "http":  # pragma: no cover - lifespan events
        return
    path = scope.get("path", "")
    if path not in ("/metrics", "/"):
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"not found"})
        return
    body = generate_latest(REGISTRY)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", CONTENT_TYPE_LATEST.encode("latin-1"))],
        }
    )
    await send({"type": "http.response.body", "body": body})


def metrics_asgi_app() -> Callable[..., Awaitable[None]]:
    """Return the ASGI app that serves ``/metrics`` on the metrics port."""
    return _metrics_asgi_app


def start_metrics_server(port: int = 9090, host: str = "0.0.0.0") -> asyncio.Task[None]:  # noqa: S104 — cluster-internal
    """Start an asyncio-managed uvicorn instance serving ``/metrics`` on ``port``.

    Returns the background task handle so callers can cancel on shutdown.
    Cluster-internal only; must never be exposed to the internet.
    """
    global _METRICS_SERVER_TASK  # noqa: PLW0603 - process singleton

    if _METRICS_SERVER_TASK is not None and not _METRICS_SERVER_TASK.done():
        return _METRICS_SERVER_TASK

    try:
        import uvicorn
    except ImportError:  # pragma: no cover — uvicorn ships as a FastAPI dep
        logger.warning("uvicorn not installed; metrics server disabled")

        async def _noop() -> None:
            return None

        _METRICS_SERVER_TASK = asyncio.get_event_loop().create_task(_noop())
        return _METRICS_SERVER_TASK

    config = uvicorn.Config(
        app=_metrics_asgi_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    _METRICS_SERVER_TASK = asyncio.create_task(server.serve(), name="caretaker-metrics-server")
    logger.info("Prometheus metrics server listening on %s:%d/metrics", host, port)
    return _METRICS_SERVER_TASK


async def stop_metrics_server() -> None:
    """Cancel the background metrics server (called from lifespan teardown)."""
    global _METRICS_SERVER_TASK  # noqa: PLW0603 - process singleton
    task = _METRICS_SERVER_TASK
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task
    _METRICS_SERVER_TASK = None


# ── Sync timing decorator for DB call sites ──────────────────────────

F = TypeVar("F", bound="Callable[..., Any]")


def timed_op(*, db_system: str, operation: str) -> Callable[[F], F]:
    """Decorator that records ``db_client_*`` metrics around a call site.

    Works on both sync and async callables. The decorated function's
    return value is passed through unchanged; exceptions re-raise after
    recording an ``outcome="failure"`` sample.
    """

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                outcome = "success"
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    outcome = "failure"
                    raise
                finally:
                    _record_db_op(db_system, operation, outcome, time.perf_counter() - start)

            return _async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            outcome = "success"
            try:
                return func(*args, **kwargs)
            except Exception:
                outcome = "failure"
                raise
            finally:
                _record_db_op(db_system, operation, outcome, time.perf_counter() - start)

        return _sync_wrapper  # type: ignore[return-value]

    return decorator


def _record_db_op(db_system: str, operation: str, outcome: str, duration: float) -> None:
    DB_CLIENT_OPERATIONS_TOTAL.labels(
        service=_SERVICE_LABEL,
        db_system=db_system,
        db_operation=operation,
        outcome=outcome,
    ).inc()
    DB_CLIENT_OPERATION_DURATION_SECONDS.labels(
        service=_SERVICE_LABEL,
        db_system=db_system,
        db_operation=operation,
    ).observe(duration)


# ── Recording helpers invoked by non-decorator call sites ────────────


def record_http_client(
    peer_service: str,
    method: str,
    status_code: int,
    duration: float,
) -> None:
    """Record an outbound HTTP call. Called by :mod:`caretaker.github_client`."""
    code_label = _bucket_status(status_code)
    HTTP_CLIENT_REQUESTS_TOTAL.labels(
        service=_SERVICE_LABEL,
        peer_service=peer_service,
        http_method=method.upper(),
        http_status_code=code_label,
    ).inc()
    HTTP_CLIENT_REQUEST_DURATION_SECONDS.labels(
        service=_SERVICE_LABEL,
        peer_service=peer_service,
        http_method=method.upper(),
        http_status_code=code_label,
    ).observe(duration)


def record_worker_job(job: str, outcome: str, duration: float) -> None:
    """Record a single worker/agent job completion."""
    WORKER_JOBS_TOTAL.labels(service=_SERVICE_LABEL, job=job, outcome=outcome).inc()
    WORKER_JOB_DURATION_SECONDS.labels(service=_SERVICE_LABEL, job=job, outcome=outcome).observe(
        duration
    )


def set_worker_queue_depth(queue: str, depth: int) -> None:
    """Update the ``worker_queue_depth`` gauge for ``queue``."""
    WORKER_QUEUE_DEPTH.labels(service=_SERVICE_LABEL, queue=queue).set(float(depth))


def record_webhook_event(event: str, mode: str, outcome: str) -> None:
    """Record a single GitHub webhook delivery handled by the dispatcher.

    ``event`` is the ``X-GitHub-Event`` header value (bounded by the
    GitHub API event catalog). ``mode`` is one of ``off``/``shadow``/
    ``active``. ``outcome`` is one of ``dispatched``/``no_agents``/
    ``duplicate``/``error``.
    """
    WEBHOOK_EVENTS_TOTAL.labels(
        service=_SERVICE_LABEL,
        event=event,
        mode=mode,
        outcome=outcome,
    ).inc()


def record_error(kind: str) -> None:
    """Record a single classified caretaker error (bounded enum ``kind``)."""
    CARETAKER_ERRORS_TOTAL.labels(service=_SERVICE_LABEL, kind=kind).inc()


def record_orchestrator_soft_fail(category: str = "transient") -> None:
    """Record an orchestrator run that soft-failed (exit 0 despite errors).

    ``category`` is a bounded enum; keep it short and stable so the label
    cardinality never escapes a handful of values.
    """
    ORCHESTRATOR_SOFT_FAIL_TOTAL.labels(service=_SERVICE_LABEL, category=category).inc()


def set_rate_limit_cooldown(peer_service: str, seconds_remaining: float) -> None:
    """Publish the current rate-limit cooldown window size (seconds)."""
    RATE_LIMIT_COOLDOWN_SECONDS.labels(service=_SERVICE_LABEL, peer_service=peer_service).set(
        max(0.0, seconds_remaining)
    )


def set_rate_limit_remaining(peer_service: str, remaining: int) -> None:
    """Publish the last ``X-RateLimit-Remaining`` value for ``peer_service``."""
    RATE_LIMIT_REMAINING.labels(service=_SERVICE_LABEL, peer_service=peer_service).set(
        float(remaining)
    )


def record_github_scope_gap(scope: str) -> None:
    """Record a single 403 caused by a missing GitHub token scope.

    ``scope`` must be one of the bounded scope-hint values produced by
    :func:`caretaker.github_client.scope_gap.infer_scope_hint`.
    """
    GITHUB_SCOPE_GAP_TOTAL.labels(service=_SERVICE_LABEL, scope=scope).inc()


def record_pr_outcome(repo: str, outcome: str) -> None:
    """Increment the ``caretaker_pr_outcome_total`` counter.

    ``outcome`` must be one of :data:`PR_OUTCOMES` — unknown values are
    silently dropped so a typo in the caller can never expand cardinality.
    """
    if outcome not in PR_OUTCOMES:
        logger.warning("record_pr_outcome called with unknown outcome=%r; dropping", outcome)
        return
    CARETAKER_PR_OUTCOME_TOTAL.labels(service=_SERVICE_LABEL, repo=repo, outcome=outcome).inc()


def record_issue_outcome(repo: str, outcome: str) -> None:
    """Increment the ``caretaker_issue_outcome_total`` counter."""
    if outcome not in ISSUE_OUTCOMES:
        logger.warning("record_issue_outcome called with unknown outcome=%r; dropping", outcome)
        return
    CARETAKER_ISSUE_OUTCOME_TOTAL.labels(service=_SERVICE_LABEL, repo=repo, outcome=outcome).inc()


def record_operator_intervention(repo: str, reason: str) -> None:
    """Increment the ``caretaker_operator_intervention_total`` counter."""
    if reason not in INTERVENTION_REASONS:
        logger.warning(
            "record_operator_intervention called with unknown reason=%r; dropping",
            reason,
        )
        return
    CARETAKER_OPERATOR_INTERVENTION_TOTAL.labels(
        service=_SERVICE_LABEL, repo=repo, reason=reason
    ).inc()


def record_guardrail_sanitize(source: str, modification_type: str) -> None:
    """Record one sanitize-time modification (per source × type)."""
    GUARDRAIL_SANITIZE_TOTAL.labels(
        service=_SERVICE_LABEL,
        source=source,
        modification_type=modification_type,
    ).inc()


def record_guardrail_filter_blocked(target: str, reason: str) -> None:
    """Record one filter_output rejection (per target × reason)."""
    GUARDRAIL_FILTER_BLOCKED_TOTAL.labels(
        service=_SERVICE_LABEL,
        target=target,
        reason=reason,
    ).inc()


def record_guardrail_rollback_fired(repo: str, reason: str) -> None:
    """Record one checkpoint_and_rollback rollback firing."""
    GUARDRAIL_ROLLBACK_FIRED_TOTAL.labels(
        service=_SERVICE_LABEL,
        repo=repo,
        reason=reason,
    ).inc()


def record_llm_cache_usage(
    *,
    provider: str,
    model: str,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> None:
    """Record Anthropic prompt-cache token counts from a response's ``usage``.

    Both inputs are **cumulative token counts** for a single completion, as
    surfaced in ``usage.cache_read_input_tokens`` and
    ``usage.cache_creation_input_tokens`` (Anthropic) or the LiteLLM-proxied
    equivalents.  Non-positive values are silently ignored so providers that
    don't support caching (or requests that skipped the cache) don't pollute
    the counters with zero-valued increments.
    """
    if cache_read_input_tokens > 0:
        LLM_CACHE_READ_TOKENS_TOTAL.labels(provider=provider, model=model).inc(
            float(cache_read_input_tokens)
        )
    if cache_creation_input_tokens > 0:
        LLM_CACHE_CREATION_TOKENS_TOTAL.labels(provider=provider, model=model).inc(
            float(cache_creation_input_tokens)
        )


__all__ = [
    "APP_INFO",
    "CARETAKER_ERRORS_TOTAL",
    "CARETAKER_ISSUE_OUTCOME_TOTAL",
    "CARETAKER_OPERATOR_INTERVENTION_TOTAL",
    "CARETAKER_PR_OUTCOME_TOTAL",
    "DB_CLIENT_OPERATIONS_TOTAL",
    "DB_CLIENT_OPERATION_DURATION_SECONDS",
    "GITHUB_SCOPE_GAP_TOTAL",
    "GUARDRAIL_FILTER_BLOCKED_TOTAL",
    "GUARDRAIL_ROLLBACK_FIRED_TOTAL",
    "GUARDRAIL_SANITIZE_TOTAL",
    "HTTP_CLIENT_REQUESTS_TOTAL",
    "HTTP_CLIENT_REQUEST_DURATION_SECONDS",
    "HTTP_SERVER_REQUESTS_TOTAL",
    "HTTP_SERVER_REQUEST_DURATION_SECONDS",
    "INTERVENTION_REASONS",
    "ISSUE_OUTCOMES",
    "LATENCY_BUCKETS",
    "LLM_CACHE_CREATION_TOKENS_TOTAL",
    "LLM_CACHE_READ_TOKENS_TOTAL",
    "ORCHESTRATOR_SOFT_FAIL_TOTAL",
    "PR_OUTCOMES",
    "RATE_LIMIT_COOLDOWN_SECONDS",
    "RATE_LIMIT_REMAINING",
    "REGISTRY",
    "WEBHOOK_EVENTS_TOTAL",
    "WORKER_JOBS_TOTAL",
    "WORKER_JOB_DURATION_SECONDS",
    "WORKER_QUEUE_DEPTH",
    "get_service_label",
    "init_metrics",
    "metrics_asgi_app",
    "record_error",
    "record_github_scope_gap",
    "record_guardrail_filter_blocked",
    "record_guardrail_rollback_fired",
    "record_guardrail_sanitize",
    "record_http_client",
    "record_issue_outcome",
    "record_llm_cache_usage",
    "record_operator_intervention",
    "record_orchestrator_soft_fail",
    "record_pr_outcome",
    "record_webhook_event",
    "record_worker_job",
    "set_rate_limit_cooldown",
    "set_rate_limit_remaining",
    "set_worker_queue_depth",
    "start_metrics_server",
    "stop_metrics_server",
    "timed_op",
]
