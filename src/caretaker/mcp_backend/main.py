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

from caretaker.github_app import (
    WebhookSignatureError,
    agents_for_event,
    parse_webhook,
    verify_signature,
)
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
    # M8 of the memory-graph plan — initialise OpenTelemetry GenAI
    # tracing. A no-op when the ``otel`` extra is missing or
    # ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, so the default install
    # doesn't pay the SDK cost. ``init_tracing`` never raises.
    try:
        from caretaker.observability import init_tracing

        init_tracing("caretaker-mcp")
    except Exception:
        logger.debug("OpenTelemetry init skipped", exc_info=True)

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

    # Unconditionally register the unauthenticated fleet-heartbeat
    # receiver so consumer caretaker runs can register themselves
    # regardless of whether the full admin dashboard is enabled on
    # this backend instance. The corresponding *admin* list endpoints
    # are mounted inside the admin branch below.
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
    """Basic health probe for Kubernetes or Container Apps."""
    return {"status": "ok", "version": app.version}


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

    In Phase 1 this endpoint only *records* deliveries — it does not yet
    run agents.  Phase 2 pilots the security agent by wiring the matching
    agent name(s) from :func:`agents_for_event` into the orchestrator.
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

    logger.info(
        "webhook accepted event=%s delivery=%s action=%s installation=%s "
        "repository=%s duplicate=%s agents=%s",
        parsed.event_type,
        parsed.delivery_id,
        parsed.action,
        parsed.installation_id,
        parsed.repository_full_name,
        not is_new,
        agents,
    )

    return WebhookAck(
        status="accepted",
        event=parsed.event_type,
        delivery_id=parsed.delivery_id,
        duplicate=not is_new,
        agents=agents,
        installation_id=parsed.installation_id,
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
