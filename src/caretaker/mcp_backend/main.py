"""Minimal backend service scaffolding for Caretaker MCP endpoints.

This FastAPI app hosts two logical surfaces:

1. The MCP tool interface (``/health``, ``/mcp/tools``, ``/mcp/tools/call``)
   described in ``docs/azure-mcp-architecture-plan.md``.
2. The optional GitHub App front-end (``/webhooks/github``,
   ``/oauth/callback``) described in ``docs/github-app-plan.md``.  These
   routes are always registered but return ``503 Service Unavailable``
   when the corresponding environment variables are not configured,
   preserving backward compatibility for existing deployments.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
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
    _PKG_VERSION = importlib.metadata.version("caretaker")
except importlib.metadata.PackageNotFoundError:
    _PKG_VERSION = "0.0.0"

app = FastAPI(
    title="Caretaker MCP Backend",
    description=(
        "Backend service for remote Caretaker capabilities.  Hosts the MCP "
        "tool interface and (optionally) the GitHub App webhook receiver."
    ),
    version=_PKG_VERSION,
)


# ── Delivery dedup ───────────────────────────────────────────────────
#
# Uses Redis (via REDIS_URL) when available so dedup works correctly across
# multiple replicas.  Falls back to an in-process LRU set for single-replica
# deployments and local development.
#
# Free SaaS options: Upstash (upstash.com), Redis Cloud (redis.io/cloud).

_dedup: RedisDedup | LocalDedup = build_dedup()


async def _remember_delivery(delivery_id: str) -> bool:
    """Return ``True`` if this delivery id is new, ``False`` if it is a retry."""
    return await _dedup.is_new(delivery_id)


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


# Entrypoint for local testing:
# uvicorn src.caretaker.mcp_backend.main:app --reload
