"""Minimal backend service scaffolding for Caretaker MCP endpoints.

This FastAPI app hosts three logical surfaces:

1. The MCP tool interface (``/health``, ``/mcp/tools``, ``/mcp/tools/call``)
   described in ``docs/azure-mcp-architecture-plan.md``.
2. The optional GitHub App front-end (``/webhooks/github``,
   ``/oauth/callback``) described in ``docs/github-app-plan.md``.  These
   routes are always registered but return ``503 Service Unavailable``
   when the corresponding environment variables are not configured,
   preserving backward compatibility for existing deployments.
3. The admin dashboard (``/api/auth/*``, ``/api/admin/*``, ``/api/graph/*``)
   with OIDC authentication, read-only data access, and a React SPA
   served as static files at the root.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from caretaker.eventbus import (
    DEFAULT_STREAM as _BUS_STREAM,
)
from caretaker.eventbus import (
    EventBus,
    EventBusError,
    build_event_bus,
    start_webhook_consumer,
    webhook_event_payload,
)
from caretaker.github_app import (
    DispatchMode,
    GitHubAppContextFactory,
    RegistryAgentRunner,
    WebhookDispatcher,
    WebhookSignatureError,
    agents_for_event,
    dispatch_in_background,
    parse_webhook,
    verify_signature,
)
from caretaker.github_client.rate_limit import get_cooldown
from caretaker.observability.metrics import record_error, record_webhook_event
from caretaker.state.dedup import LocalDedup, RedisDedup, build_dedup
from caretaker.state.token_broker import build_token_broker

logger = logging.getLogger(__name__)

try:
    _PKG_VERSION = importlib.metadata.version("caretaker-github")
except importlib.metadata.PackageNotFoundError:
    _PKG_VERSION = "0.0.0"


# ---------------------------------------------------------------------------
# Admin dashboard bootstrap (OIDC + data access + graph API)
# ---------------------------------------------------------------------------

_ADMIN_STATIC_DIR = Path(__file__).resolve().parent.parent / "admin" / "static"


@asynccontextmanager
async def _lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
    """App lifespan: initialise admin dashboard if configured."""
    # End-to-end OpenTelemetry tracing — wires the OTLP exporter, the
    # FastAPI/httpx/Redis/Neo4j auto-instrumentors, and the logging
    # instrumentor that stamps trace_id/span_id onto every LogRecord.
    # No-op when the ``otel`` extra is missing or
    # ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset.
    try:
        from caretaker.observability import bootstrap_observability

        bootstrap_observability("caretaker-mcp")
    except Exception:
        logger.debug("OpenTelemetry bootstrap skipped", exc_info=True)

    # Prometheus metrics (paved-path SKILL §1-§10). RED-floor HTTP
    # metrics + http_client_* + db_client_* + worker_* are emitted
    # unconditionally; /metrics is served on a separate cluster-internal
    # port (default 9090) so scraping never contends with user traffic.
    try:
        from caretaker.observability import init_metrics, start_metrics_server

        init_metrics(application, service="caretaker-mcp")
        metrics_port = int(os.environ.get("CARETAKER_METRICS_PORT", "9090"))
        # Opt out by setting the port to 0 — useful for in-process
        # tests that don't want a background asyncio server.
        if metrics_port > 0:
            application.state.metrics_server_task = start_metrics_server(port=metrics_port)
    except Exception:
        logger.warning("Prometheus metrics init failed", exc_info=True)

    # Configure shared OAuth2 bearer-token auth used by the fleet
    # heartbeat endpoint (and any future authenticated public
    # resources). All caretaker clients authenticate via OAuth2
    # client_credentials against the configured OIDC issuer, so a
    # single ``CARETAKER_OIDC_ISSUER_URL`` env var configures both the
    # fleet heartbeat verifier and any other Bearer-authenticated
    # endpoints. We fall back to ``CARETAKER_ADMIN_OIDC_ISSUER_URL``
    # (the historical admin-only env var) for deployments that already
    # set it; in greenfield deployments only the unified env name is
    # required.
    try:
        from caretaker.auth import bearer as fleet_bearer

        issuer_url = (
            os.environ.get("CARETAKER_OIDC_ISSUER_URL", "").strip()
            or os.environ.get("CARETAKER_ADMIN_OIDC_ISSUER_URL", "").strip()
        )
        if issuer_url:
            try:
                await fleet_bearer.configure(
                    issuer_url=issuer_url,
                    required_scopes=("fleet:heartbeat",),
                )
                logger.info(
                    "Fleet bearer auth configured (issuer=%s, required_scope=fleet:heartbeat)",
                    issuer_url,
                )
            except Exception:
                logger.exception(
                    "Failed to configure fleet bearer auth against issuer %s; "
                    "fleet heartbeat endpoint will reject all requests with 503",
                    issuer_url,
                )
        else:
            logger.warning(
                "CARETAKER_OIDC_ISSUER_URL (and CARETAKER_ADMIN_OIDC_ISSUER_URL) not set — "
                "fleet bearer auth not configured; heartbeats will be rejected with 503"
            )
    except Exception:
        logger.exception("Failed to import fleet bearer auth module")

    # ── GitHub Actions OIDC + streamed-runs router ───────────────────
    #
    # When ``CARETAKER_OIDC_GITHUB_AUDIENCE`` is set, the backend accepts
    # short-lived OIDC tokens from GitHub Actions runners as proof of
    # workflow identity — replacing the long-lived PATs that consumer
    # workflows previously needed. The runs router exposes the
    # /runs/start, /runs/{id}/{logs,trigger,heartbeat,finish,stream}
    # endpoints used by the runner-side ``caretaker stream`` shipper.
    try:
        from caretaker.auth import github_oidc as gha_oidc

        gh_audience = os.environ.get("CARETAKER_OIDC_GITHUB_AUDIENCE", "").strip()
        if gh_audience:
            try:
                await gha_oidc.configure_github_oidc(audience=gh_audience)
                logger.info("GitHub Actions OIDC configured (audience=%s)", gh_audience)
            except Exception:
                logger.exception(
                    "Failed to configure GitHub Actions OIDC; /runs endpoints "
                    "will reject all callers with 401",
                )
        else:
            logger.info(
                "CARETAKER_OIDC_GITHUB_AUDIENCE not set — /runs/start will reject "
                "all GitHub Actions OIDC tokens",
            )
    except Exception:
        logger.exception("Failed to import github_oidc auth module")

    try:
        from caretaker.runs import api as runs_api
        from caretaker.runs import dispatch as runs_dispatch
        from caretaker.runs.store import get_store as _get_runs_store

        # Install the contextvar-scoped log handler that streams agent
        # output into per-run Redis streams. Idempotent.
        runs_dispatch.install_log_handler(_get_runs_store())

        # Wire repo→installation resolver if the GitHub App is configured.
        resolver = None
        if _token_broker is not None:
            from caretaker.github_app.repo_installation import RepoInstallationResolver

            resolver = RepoInstallationResolver(signer=_token_broker._signer)
            await resolver.__aenter__()
            application.state.runs_resolver = resolver

            async def _check(repo: str) -> int | None:
                return await resolver.get(repo)

            gha_oidc.set_installation_check(_check)
            logger.info("GitHub App installation check wired into runs OIDC validation")

        # Configure dispatch bridge — used by /runs/{id}/trigger.
        # event_bus_factory routes triggers through the durable event
        # bus so they survive pod restarts; falls back to in-process if
        # the bus publish raises.
        runs_dispatch.configure(
            resolver=resolver,
            token_broker=_token_broker,
            dispatcher_factory=_get_dispatcher,
            event_bus_factory=_get_event_bus,
        )
        runs_api.configure_dispatch(runs_dispatch.run_trigger)
        application.include_router(runs_api.router)
        logger.info("Streamed-runs API mounted at /runs")
    except Exception:
        logger.exception("Failed to mount streamed-runs API")

    # Register the OAuth2-protected fleet-heartbeat receiver so
    # opted-in caretaker runs can register themselves regardless of
    # whether the full admin dashboard is enabled on this backend
    # instance. The corresponding *admin* list endpoints are mounted
    # inside the admin branch below.
    try:
        from caretaker.fleet import public_router as fleet_public_router

        application.include_router(fleet_public_router)
        logger.info("Fleet registry heartbeat endpoint enabled")
    except Exception:
        logger.warning("Failed to initialise fleet heartbeat endpoint", exc_info=True)

    admin_enabled = os.environ.get("CARETAKER_ADMIN_ENABLED", "").lower() in ("1", "true", "yes")

    if admin_enabled:
        try:
            from caretaker.admin import api as admin_api
            from caretaker.admin import auth as admin_auth
            from caretaker.admin.data import AdminDataAccess
            from caretaker.config import AdminDashboardConfig

            # Build config from env vars
            config = AdminDashboardConfig(
                enabled=True,
                oidc_issuer_url=os.environ.get("CARETAKER_ADMIN_OIDC_ISSUER_URL", ""),
                public_base_url=os.environ.get("CARETAKER_ADMIN_PUBLIC_BASE_URL", ""),
                allowed_emails=[
                    e.strip()
                    for e in os.environ.get("CARETAKER_ADMIN_ALLOWED_EMAILS", "").split(",")
                    if e.strip()
                ],
            )

            if config.oidc_issuer_url:
                await admin_auth.configure(config)
                logger.info("Admin OIDC auth configured (issuer=%s)", config.oidc_issuer_url)
            else:
                logger.warning("CARETAKER_ADMIN_OIDC_ISSUER_URL not set — admin auth disabled")

            # Initialise data access. The state store is hydrated by a
            # background task (see admin.state_loader) that polls the
            # orchestrator's per-repo tracking issue.
            data = AdminDataAccess()
            admin_api.configure(data)

            try:
                from caretaker.admin.state_loader import build_refresh_task

                application.state.admin_refresh_task = build_refresh_task(data)
            except Exception:
                logger.warning("Failed to start admin state refresh task", exc_info=True)
                application.state.admin_refresh_task = None

            application.include_router(admin_auth.router)
            application.include_router(admin_api.router)

            # Fleet registry — authenticated list endpoints for the
            # admin dashboard. The public heartbeat receiver is
            # registered below (outside the admin gate) so consumer CI
            # runners can post without a session cookie.
            try:
                from caretaker.fleet import admin_router as fleet_admin_router

                application.include_router(fleet_admin_router)
                logger.info("Fleet registry admin API enabled")
            except Exception:
                logger.warning("Failed to initialise fleet admin API", exc_info=True)

            # Kubernetes agent-worker admin endpoints. Opt-in via
            # ``executor.k8s_worker.enabled`` in the config; defaults to
            # off so the MCP backend doesn't need the optional
            # ``kubernetes`` Python package unless the feature is on.
            try:
                from caretaker.config import MaintainerConfig
                from caretaker.k8s_worker import (
                    K8sAgentLauncher,
                )
                from caretaker.k8s_worker import (
                    configure as configure_k8s,
                )
                from caretaker.k8s_worker import (
                    router as k8s_router,
                )

                maint_cfg_path = os.environ.get(
                    "CARETAKER_CONFIG_PATH", ".github/maintainer/config.yml"
                )
                try:
                    maint_cfg = MaintainerConfig.from_yaml(maint_cfg_path)
                except Exception:
                    maint_cfg = MaintainerConfig()
                worker_cfg = maint_cfg.executor.k8s_worker
                if worker_cfg.enabled:
                    redis_client = None
                    redis_url = os.environ.get("REDIS_URL", "").strip()
                    if redis_url:
                        try:
                            from redis.asyncio import Redis as AsyncRedis

                            redis_client = AsyncRedis.from_url(redis_url)
                        except Exception:
                            logger.warning(
                                "k8s_worker: Redis unavailable, dedupe disabled",
                                exc_info=True,
                            )
                    launcher = K8sAgentLauncher(config=worker_cfg, redis=redis_client)
                    configure_k8s(launcher, worker_cfg)
                    application.include_router(k8s_router)
                    logger.info(
                        "K8s agent-worker admin API enabled (namespace=%s)",
                        worker_cfg.namespace,
                    )
            except Exception:
                logger.warning("Failed to initialise k8s agent-worker admin API", exc_info=True)

            # Mount graph API if Neo4j is configured
            neo4j_url = os.environ.get("NEO4J_URL", "")
            if neo4j_url:
                try:
                    from caretaker.admin.graph_api import (
                        admin_router as graph_admin_router,
                    )
                    from caretaker.admin.graph_api import configure as configure_graph
                    from caretaker.admin.graph_api import router as graph_router

                    await configure_graph()
                    application.include_router(graph_router)
                    # M4 compaction endpoint — lives on ``/api/admin``
                    # but shares the same store handle as the graph
                    # router so we mount it in the same branch.
                    application.include_router(graph_admin_router)
                    logger.info("Graph API enabled (Neo4j at %s)", neo4j_url)
                except Exception:
                    logger.warning("Failed to initialise graph API", exc_info=True)

            # MCP memory adapter (M5) — wires the existing causal store
            # plus (when present) the graph + insight stores into the
            # read-only HTTP surface described in
            # ``docs/memory-graph-plan.md`` §4.4. Each endpoint returns
            # 503 independently when its backing store is unset, so a
            # partially configured backend still serves whatever is
            # available.
            try:
                from caretaker.mcp_backend import memory_tools

                try:
                    from caretaker.admin.graph_api import _store as _graph_store_mod
                except Exception:
                    _graph_store_mod = None

                memory_tools.configure(
                    graph_store=_graph_store_mod,
                    causal_store=data.causal_store,
                    insight_store=getattr(data, "_insights", None),
                )
                application.include_router(memory_tools.router)
                logger.info("MCP memory adapter enabled")
            except Exception:
                logger.warning("Failed to initialise MCP memory adapter", exc_info=True)

            # Health/doctor endpoint — surfaces aggregated bootstrap
            # status (GitHub creds, OIDC, admin data, graph store, fleet
            # store, Neo4j URI, fleet secret) at /health/doctor for the
            # admin dashboard's "system health" panel.
            try:
                from caretaker.admin import health_api
                from caretaker.fleet import get_store as _get_fleet_store

                try:
                    _graph_for_health = _graph_store_mod
                except NameError:
                    _graph_for_health = None

                health_api.configure(
                    admin_data=data,
                    graph_store=_graph_for_health,
                    fleet_store=_get_fleet_store(),
                )
                application.include_router(health_api.router)
                logger.info("Admin health/doctor API enabled")
            except Exception:
                logger.warning("Failed to initialise admin health API", exc_info=True)

            # Streamed-runs admin surface — list, detail, SSE feed +
            # background sweeper that flips long-silent runs to ``stalled``.
            try:
                from caretaker.admin import runs_stream_api

                application.include_router(runs_stream_api.router)
                application.state.runs_sweeper_task = runs_stream_api.build_sweeper_task()
                logger.info("Admin runs API + sweeper enabled")
            except Exception:
                logger.warning("Failed to initialise admin runs API", exc_info=True)

            # Webhook delivery history — exposes the in-process ring
            # buffer populated by the GitHub webhook handler so the
            # admin dashboard can show recent deliveries (event, action,
            # repo, installation, status). The webhook handler below
            # calls ``register_delivery()`` after acking each request.
            try:
                from caretaker.admin import webhooks_api

                application.include_router(webhooks_api.router)
                logger.info("Admin webhook deliveries API enabled")
            except Exception:
                logger.warning("Failed to initialise admin webhooks API", exc_info=True)

            # Serve SPA static files
            if _ADMIN_STATIC_DIR.is_dir():
                application.mount(
                    "/assets",
                    StaticFiles(directory=str(_ADMIN_STATIC_DIR / "assets")),
                    name="admin-assets",
                )
                logger.info("Admin SPA static files served from %s", _ADMIN_STATIC_DIR)

            logger.info("Admin dashboard enabled")

        except Exception:
            logger.warning("Failed to initialise admin dashboard", exc_info=True)

    # ── Webhook event-bus consumer ───────────────────────────────────
    #
    # One consume task + one reaper task per replica. The consumer group
    # gives us at-least-once delivery, automatic load-balancing across
    # replicas, and (via the reaper's XAUTOCLAIM) redelivery of messages
    # whose handler crashed mid-processing. When Redis is not configured
    # the bus falls back to in-process; the consumer still runs but
    # only sees messages produced inside this same process.
    try:
        bus = _get_event_bus()
        dispatcher = _get_dispatcher()
        consume_task, reaper_task = start_webhook_consumer(
            bus=bus,
            dispatcher=dispatcher,
        )
        application.state.eventbus = bus
        application.state.eventbus_consume_task = consume_task
        application.state.eventbus_reaper_task = reaper_task
        logger.info("Webhook event-bus consumer started")
    except Exception:
        logger.exception("Failed to start webhook event-bus consumer")
        application.state.eventbus = None
        application.state.eventbus_consume_task = None
        application.state.eventbus_reaper_task = None

    # ── Reconciliation scheduler ─────────────────────────────────────
    #
    # Replaces the per-repo cron in the heavy maintainer.yml with a
    # single in-cluster schedule. Fans out a synthetic ``schedule`` event
    # per installed repo onto the event bus on each tick. Multi-pod
    # safety via Redis lease — only one replica fires per tick.
    #
    # Disabled by default to keep test environments quiet; opt in via
    # ``CARETAKER_SCHEDULER_ENABLED=true`` in production.
    application.state.scheduler_task = None
    application.state.installations_index = None
    scheduler_enabled = os.environ.get("CARETAKER_SCHEDULER_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if scheduler_enabled:
        try:
            if _token_broker is None:
                logger.warning(
                    "CARETAKER_SCHEDULER_ENABLED=true but GitHub App is not configured; "
                    "skipping scheduler"
                )
            else:
                from caretaker.github_app.installations_index import InstallationsIndex
                from caretaker.scheduler import start_reconciliation_scheduler

                bus = getattr(application.state, "eventbus", None) or _get_event_bus()
                index = InstallationsIndex(
                    signer=_token_broker._signer,
                    token_minter=_token_broker,
                )
                await index.__aenter__()
                application.state.installations_index = index
                application.state.scheduler_task = start_reconciliation_scheduler(
                    bus=bus,
                    installations_index=index,
                )
                logger.info("Reconciliation scheduler started")
        except Exception:
            logger.exception("Failed to start reconciliation scheduler")

    # Register the SPA catch-all AFTER all API routes so it never shadows them.
    # Starlette matches routes in registration order; a /{full_path:path} wildcard
    # added at module level would intercept every /api/* request before the admin
    # router (added above) gets a chance to handle it.
    @application.get("/{full_path:path}", include_in_schema=False)
    async def spa_catchall(full_path: str) -> Response:
        """Serve the admin SPA index.html for any unmatched route."""
        index = _ADMIN_STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(str(index), media_type="text/html")
        return Response(content="Admin dashboard not built", status_code=404)

    yield

    task = getattr(application.state, "admin_refresh_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    sweeper = getattr(application.state, "runs_sweeper_task", None)
    if sweeper is not None:
        sweeper.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await sweeper

    # Cancel the event-bus consumer + reaper, then close the bus client.
    for attr in ("eventbus_consume_task", "eventbus_reaper_task", "scheduler_task"):
        task_handle = getattr(application.state, attr, None)
        if task_handle is not None:
            task_handle.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task_handle
    bus_attr: EventBus | None = getattr(application.state, "eventbus", None)
    if bus_attr is not None:
        with suppress(Exception):
            await bus_attr.close()
    installations_index = getattr(application.state, "installations_index", None)
    if installations_index is not None:
        with suppress(Exception):
            await installations_index.__aexit__(None, None, None)

    runs_resolver = getattr(application.state, "runs_resolver", None)
    if runs_resolver is not None:
        with suppress(Exception):
            await runs_resolver.__aexit__(None, None, None)

    with suppress(Exception):
        from caretaker.runs.store import get_store as _runs_get_store

        await _runs_get_store().close()

    # Stop the metrics server side-car cleanly so tests that re-enter
    # the lifespan don't leak a port binding.
    try:
        from caretaker.observability import stop_metrics_server

        await stop_metrics_server()
    except Exception:
        logger.debug("Prometheus metrics server shutdown skipped", exc_info=True)


app = FastAPI(
    title="Caretaker MCP Backend",
    description=(
        "Backend service for remote Caretaker capabilities.  Hosts the MCP "
        "tool interface, (optionally) the GitHub App webhook receiver, and "
        "the admin dashboard."
    ),
    version=_PKG_VERSION,
    lifespan=_lifespan,
)


# ── Delivery dedup ───────────────────────────────────────────────
#
# Uses Redis (via REDIS_URL) when available so dedup works correctly across
# multiple replicas.  Falls back to an in-process LRU set for single-replica
# deployments and local development.
#
# Free SaaS options: Upstash (upstash.com), Redis Cloud (redis.io/cloud).
#
# Lazily initialised on first webhook request so that importing this module
# (e.g. during unit tests) never touches Redis.

_dedup: RedisDedup | LocalDedup | None = None


def _get_dedup() -> RedisDedup | LocalDedup:
    """Return the module-level dedup singleton, building it on first call."""
    global _dedup  # noqa: PLW0603
    if _dedup is None:
        _dedup = build_dedup()
    return _dedup


async def _remember_delivery(delivery_id: str) -> bool:
    """Return ``True`` if this delivery id is new, ``False`` if it is a retry."""
    return await _get_dedup().is_new(delivery_id)


# ── Installation-token broker ─────────────────────────────────────────
#
# Lazily initialised; returns None when the GitHub App is not configured.

_token_broker = build_token_broker()


# ── Event bus (durable webhook fan-out) ──────────────────────────────
#
# Webhook deliveries are published onto a Redis Stream + consumer group
# so dispatch survives pod restarts and load-balances across replicas.
# Falls back to an in-process LocalEventBus when REDIS_URL is unset
# (single-pod dev). The webhook handler retains a fallback to
# ``dispatch_in_background`` if publish fails — the goal is that a Redis
# outage degrades to MVP behaviour instead of returning 5xx to GitHub.

_event_bus: EventBus | None = None


def _get_event_bus() -> EventBus:
    global _event_bus  # noqa: PLW0603
    if _event_bus is None:
        _event_bus = build_event_bus()
    return _event_bus


# ── Webhook dispatcher (Phase 2) ──────────────────────────────────────
#
# Mode is read from ``CARETAKER_WEBHOOK_DISPATCH_MODE`` once at import
# time. Defaults to ``off`` so existing deployments are unchanged; set
# to ``shadow`` to observe real traffic before enabling execution.

_dispatcher: WebhookDispatcher | None = None


def _get_dispatcher() -> WebhookDispatcher:
    """Return the module-level dispatcher singleton, building on first use.

    Active mode requires the GitHub App to be configured (``_token_broker``
    must not be ``None``). If the App is unconfigured and mode is ``active``,
    the dispatcher falls back to ``off`` with a warning so the webhook handler
    keeps returning 202s rather than crashing.
    """
    global _dispatcher  # noqa: PLW0603
    if _dispatcher is None:
        mode = DispatchMode.parse(os.environ.get("CARETAKER_WEBHOOK_DISPATCH_MODE"))

        context_factory = None
        agent_runner = None
        active_agents: frozenset[str] | None = None

        if mode is DispatchMode.ACTIVE:
            if _token_broker is None:
                logger.warning(
                    "CARETAKER_WEBHOOK_DISPATCH_MODE=active but GitHub App is not "
                    "configured; downgrading to 'off'. Set CARETAKER_GITHUB_APP_ID "
                    "and CARETAKER_GITHUB_APP_PRIVATE_KEY to enable active dispatch."
                )
                mode = DispatchMode.OFF
            else:
                from caretaker.config import MaintainerConfig
                from caretaker.llm.router import LLMRouter

                cfg_path = os.environ.get("CARETAKER_CONFIG_PATH", ".github/maintainer/config.yml")
                try:
                    default_cfg = MaintainerConfig.from_yaml(cfg_path)
                except Exception:
                    logger.info(
                        "No local maintainer config at %r; using defaults for active dispatch",
                        cfg_path,
                    )
                    default_cfg = MaintainerConfig()

                llm_router = LLMRouter(default_cfg.llm)
                dry_run = os.environ.get("CARETAKER_DRY_RUN", "").lower() in ("1", "true", "yes")

                context_factory = GitHubAppContextFactory(
                    minter=_token_broker,
                    llm_router=llm_router,
                    default_config=default_cfg,
                    dry_run=dry_run,
                )
                agent_runner = RegistryAgentRunner()

                # CARETAKER_WEBHOOK_ACTIVE_AGENTS: comma-separated allow-list,
                # e.g. "pr-reviewer,pr". When unset all resolved agents run.
                raw_active = os.environ.get("CARETAKER_WEBHOOK_ACTIVE_AGENTS", "").strip()
                if raw_active:
                    active_agents = frozenset(
                        name.strip() for name in raw_active.split(",") if name.strip()
                    )
                    logger.info(
                        "webhook active dispatch allow-list: %s",
                        sorted(active_agents),
                    )

        _dispatcher = WebhookDispatcher(
            mode=mode,
            context_factory=context_factory,
            agent_runner=agent_runner,
            active_agents=active_agents,
        )
        logger.info("webhook dispatcher initialised mode=%s", mode.value)
    return _dispatcher


# ── Models -------------------------------------------------------------


class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any]


class ToolCallResponse(BaseModel):
    status: str
    tool_name: str
    result: Any


class WebhookAck(BaseModel):
    status: str
    event: str
    delivery_id: str
    duplicate: bool
    agents: list[str]
    installation_id: int | None
    # ``True`` when the dispatcher was scheduled, ``False`` when the
    # delivery was 200-acked but agent dispatch was deliberately skipped
    # (e.g. GitHub rate-limit cooldown). Default keeps the field
    # backwards-compatible with older clients reading the schema.
    dispatched: bool = True


# ── MCP endpoints ------------------------------------------------------


def _allowed_object_ids() -> set[str]:
    raw = os.environ.get("CARETAKER_MCP_ALLOWED_OBJECT_IDS", "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _enforce_auth(
    authorization: str | None,
    principal_id: str | None,
) -> None:
    """Enforce configured auth mode for MCP endpoints.

    Modes:
    - none: no auth required
    - token: bearer token via CARETAKER_MCP_AUTH_TOKEN
    - apim: trust APIM-authenticated caller identity headers
    """
    auth_mode = os.environ.get("CARETAKER_MCP_AUTH_MODE", "none").strip().lower()

    if auth_mode == "none":
        return

    if auth_mode == "token":
        expected = os.environ.get("CARETAKER_MCP_AUTH_TOKEN", "").strip()
        if not expected:
            raise HTTPException(status_code=500, detail="Auth token not configured")

        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")

        provided = authorization.removeprefix("Bearer ").strip()
        if provided != expected:
            raise HTTPException(status_code=403, detail="Invalid bearer token")
        return

    if auth_mode == "apim":
        if not principal_id:
            raise HTTPException(
                status_code=401,
                detail="Missing APIM principal identity header",
            )

        allowed_ids = _allowed_object_ids()
        if allowed_ids and principal_id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Principal not allowed")
        return

    raise HTTPException(status_code=500, detail=f"Unsupported auth mode: {auth_mode}")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Basic health probe for Kubernetes or Container Apps.

    Includes ``dispatch_mode`` so operators can verify the live webhook
    dispatch configuration without reading env vars or restarting.
    Also reports ``github_app_configured`` so staging/prod mismatches
    are visible in monitoring dashboards.
    """
    return {
        "status": "ok",
        "version": app.version,
        "dispatch_mode": _get_dispatcher().mode.value,
        "github_app_configured": str(_token_broker is not None).lower(),
    }


@app.get("/mcp/tools")
async def list_tools(
    authorization: str | None = Header(default=None),
    x_ms_client_principal_id: str | None = Header(
        default=None,
        alias="x-ms-client-principal-id",
    ),
) -> dict[str, Any]:
    """Return the list of capabilities/tools exposed by this backend."""
    _enforce_auth(authorization=authorization, principal_id=x_ms_client_principal_id)

    return {
        "tools": [
            {
                "name": "example_tool",
                "description": "An example remote tool exposed via MCP.",
                "parameters": {
                    "type": "object",
                    "properties": {"param1": {"type": "string"}},
                },
            }
        ]
    }


@app.post("/mcp/tools/call", response_model=ToolCallResponse)
async def call_tool(
    req: ToolCallRequest,
    authorization: str | None = Header(default=None),
    x_ms_client_principal_id: str | None = Header(
        default=None,
        alias="x-ms-client-principal-id",
    ),
) -> ToolCallResponse:
    """Invoke a tool remotely."""
    logger.info("Received tool call for %s", req.tool_name)

    _enforce_auth(authorization=authorization, principal_id=x_ms_client_principal_id)

    if req.tool_name == "example_tool":
        return ToolCallResponse(
            status="success",
            tool_name=req.tool_name,
            result={
                "message": "Hello from example_tool",
                "argument_count": len(req.arguments),
                "argument_names": sorted(req.arguments.keys()),
            },
        )

    raise HTTPException(status_code=404, detail=f"Tool {req.tool_name} not found")


# ── GitHub App endpoints ----------------------------------------------


def _webhook_secret() -> str:
    """Return the webhook HMAC secret, preferring the Pydantic config env var.

    Reads the env name from ``CARETAKER_GITHUB_APP_WEBHOOK_SECRET_ENV`` if set
    (tests and advanced deployments can repoint it), otherwise reads
    ``CARETAKER_GITHUB_APP_WEBHOOK_SECRET`` directly.
    """
    env_name = os.environ.get(
        "CARETAKER_GITHUB_APP_WEBHOOK_SECRET_ENV",
        "CARETAKER_GITHUB_APP_WEBHOOK_SECRET",
    )
    return os.environ.get(env_name, "")


@app.post("/webhooks/github", response_model=WebhookAck)
async def github_webhook(request: Request) -> WebhookAck:
    """Receive, verify, and acknowledge a GitHub webhook.

    Signature + dedup run inline; agent dispatch (when the dispatcher
    is in ``shadow`` or ``active`` mode) runs in a background task so
    this handler returns well under GitHub's 10-second retry budget.
    """
    secret = _webhook_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub App webhook is not configured: set the "
                "CARETAKER_GITHUB_APP_WEBHOOK_SECRET environment variable."
            ),
        )

    raw_body = await request.body()
    try:
        verify_signature(
            secret=secret,
            body=raw_body,
            signature_header=request.headers.get("X-Hub-Signature-256"),
        )
    except WebhookSignatureError as exc:
        logger.warning("rejected webhook: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        parsed = parse_webhook(body=raw_body, headers=dict(request.headers))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    is_new = await _remember_delivery(parsed.delivery_id)
    agents = agents_for_event(parsed.event_type)

    # If the GitHub client is in a rate-limit cooldown, skip agent
    # dispatch entirely. Every dispatched task during a cooldown would
    # block on the GitHub API only to short-circuit on RateLimitError,
    # while still pinning the parsed payload + agent context in memory —
    # which is how this pod started OOMKilling under webhook bursts. The
    # webhook is still 200-acked (GitHub considers the delivery
    # successful and won't redeliver), and the missed work is recoverable
    # via the next reconcile loop / manual rerun once the cooldown clears.
    cooldown = get_cooldown()
    cooldown_blocked = is_new and cooldown.is_blocked()
    cooldown_seconds = cooldown.seconds_remaining() if cooldown_blocked else 0.0

    logger.info(
        "webhook accepted event=%s delivery=%s action=%s installation=%s "
        "repository=%s duplicate=%s agents=%s cooldown_skip=%s",
        parsed.event_type,
        parsed.delivery_id,
        parsed.action,
        parsed.installation_id,
        parsed.repository_full_name,
        not is_new,
        agents,
        cooldown_blocked,
    )

    # Mirror the delivery into the admin dashboard's recent-deliveries
    # ring buffer. Best-effort: import lazily so a missing/optional
    # admin module never blocks the webhook ack path.
    try:
        from datetime import UTC
        from datetime import datetime as _dt

        from caretaker.admin import webhooks_api as _webhooks_api

        if not is_new:
            mirror_status = "duplicate"
        elif cooldown_blocked:
            mirror_status = "deferred_cooldown"
        else:
            mirror_status = "ok"

        _webhooks_api.register_delivery(
            event=parsed.event_type,
            action=parsed.action,
            installation_id=parsed.installation_id,
            delivery_id=parsed.delivery_id,
            received_at=_dt.now(UTC).isoformat(),
            agents_fired=[] if cooldown_blocked else list(agents),
            status=mirror_status,
        )
    except Exception:
        logger.debug("webhook delivery mirror skipped", exc_info=True)

    dispatched = False
    # Only dispatch fresh deliveries — GitHub retries with the same
    # delivery id on non-2xx, and we already acked once.
    if is_new and not cooldown_blocked:
        dispatcher = _get_dispatcher()
        if dispatcher.mode is not DispatchMode.OFF:
            # Preferred path: publish onto the event bus so a consumer
            # task on this (or another) replica picks it up. Durable
            # across pod restarts; load-balanced via consumer group.
            #
            # Fallback: if publish fails (Redis outage), drop to the
            # in-process asyncio.create_task path so a Redis blip never
            # turns into a 5xx back to GitHub. The fallback is non-durable
            # — we accept that trade-off because Redis going down should
            # be rare and short.
            bus = _get_event_bus()
            try:
                await bus.publish(_BUS_STREAM, webhook_event_payload(parsed))
                dispatched = True
            except EventBusError:
                logger.warning(
                    "eventbus publish failed; falling back to in-process dispatch "
                    "event=%s delivery=%s",
                    parsed.event_type,
                    parsed.delivery_id,
                    exc_info=True,
                )
                record_error(kind="eventbus_publish_failed")
                task = dispatch_in_background(dispatcher, parsed)
                dispatched = task is not None
    elif cooldown_blocked:
        logger.warning(
            "webhook dispatch deferred: GitHub rate-limit cooldown active "
            "(%.0fs remaining) event=%s delivery=%s",
            cooldown_seconds,
            parsed.event_type,
            parsed.delivery_id,
        )
        dispatcher = _get_dispatcher()
        record_webhook_event(
            event=parsed.event_type,
            mode=dispatcher.mode.value,
            outcome="deferred_cooldown",
        )

    return WebhookAck(
        status="accepted",
        event=parsed.event_type,
        delivery_id=parsed.delivery_id,
        duplicate=not is_new,
        agents=agents,
        installation_id=parsed.installation_id,
        dispatched=dispatched,
    )


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None) -> Response:
    """OAuth user-to-server redirect callback stub.

    GitHub redirects to this URL after a user authorizes caretaker.  The
    full exchange (``POST /login/oauth/access_token``) will land in
    Phase 3; today we only validate that the route is reachable.
    """
    client_id_env = os.environ.get(
        "CARETAKER_GITHUB_APP_CLIENT_ID_ENV",
        "CARETAKER_GITHUB_APP_CLIENT_ID",
    )
    if not os.environ.get(client_id_env):
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub App OAuth is not configured: set the "
                "CARETAKER_GITHUB_APP_CLIENT_ID environment variable."
            ),
        )

    if not code:
        raise HTTPException(status_code=400, detail="missing 'code' query parameter")

    state_log = f"<redacted len={len(state)}>" if state else "<missing>"
    logger.info(
        "received oauth callback code=<redacted len=%d> state=%s",
        len(code),
        state_log,
    )
    return Response(
        content="caretaker: oauth callback received",
        media_type="text/plain",
    )


# ── Internal token-broker endpoint ------------------------------------


class TokenResponse(BaseModel):
    installation_id: int
    token: str
    expires_at: int


@app.post("/internal/tokens/installation/{installation_id}", response_model=TokenResponse)
async def get_installation_token(
    installation_id: int,
    authorization: str | None = Header(default=None),
    x_ms_client_principal_id: str | None = Header(
        default=None,
        alias="x-ms-client-principal-id",
    ),
) -> TokenResponse:
    """Return a cached GitHub App installation token.

    This endpoint is **internal-only** and must be placed behind auth
    (``CARETAKER_MCP_AUTH_MODE=token`` or ``apim``).  Agents call this
    instead of minting their own tokens so that a shared Redis cache is
    used effectively and API rate limits are respected.
    """
    _enforce_auth(authorization=authorization, principal_id=x_ms_client_principal_id)

    if _token_broker is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub App is not configured: set CARETAKER_GITHUB_APP_ID and "
                "CARETAKER_GITHUB_APP_PRIVATE_KEY (or _PATH) environment variables."
            ),
        )

    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="installation_id must be a positive integer")

    async with _token_broker as broker:
        token = await broker.get_token(installation_id)

    return TokenResponse(
        installation_id=token.installation_id,
        token=token.token,
        expires_at=token.expires_at,
    )


# ── CORS middleware (admin dashboard dev) ─────────────────────────────

_cors_origins = [
    o.strip() for o in os.environ.get("CARETAKER_ADMIN_CORS_ORIGINS", "").split(",") if o.strip()
]
if _cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


# ── SPA catchall (must be last) ──────────────────────────────────────


# Entrypoint for local testing:
# uvicorn src.caretaker.mcp_backend.main:app --reload
