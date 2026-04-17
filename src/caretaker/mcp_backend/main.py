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

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from caretaker.github_app import (
    WebhookSignatureError,
    agents_for_event,
    parse_webhook,
    verify_signature,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Caretaker MCP Backend",
    description=(
        "Backend service for remote Caretaker capabilities.  Hosts the MCP "
        "tool interface and (optionally) the GitHub App webhook receiver."
    ),
    version="0.2.0",
)


# ── Delivery dedup -----------------------------------------------------

# Process-local LRU-ish set of recently-seen ``X-GitHub-Delivery`` ids.
# GitHub retries failed webhook deliveries; dropping the retry here keeps
# downstream handlers idempotent.  A multi-replica deployment will need
# to upgrade this to Redis (see docs/azure-mcp-architecture-plan.md §2).
_DELIVERY_DEDUP_CAPACITY = 2048
_seen_deliveries: list[str] = []
_seen_deliveries_set: set[str] = set()


def _remember_delivery(delivery_id: str) -> bool:
    """Return ``True`` if this delivery id is new, ``False`` if it is a retry."""
    if delivery_id in _seen_deliveries_set:
        return False
    _seen_deliveries.append(delivery_id)
    _seen_deliveries_set.add(delivery_id)
    while len(_seen_deliveries) > _DELIVERY_DEDUP_CAPACITY:
        evicted = _seen_deliveries.pop(0)
        _seen_deliveries_set.discard(evicted)
    return True


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


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Basic health probe for Kubernetes or Container Apps."""
    return {"status": "ok", "version": app.version}


@app.get("/mcp/tools")
async def list_tools() -> dict[str, Any]:
    """Return the list of capabilities/tools exposed by this backend."""
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
async def call_tool(req: ToolCallRequest) -> ToolCallResponse:
    """Invoke a tool remotely."""
    logger.info("Received tool call for %s", req.tool_name)

    auth_mode = os.environ.get("CARETAKER_MCP_AUTH_MODE", "none")
    if auth_mode == "token":
        # Placeholder for token validation
        pass

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

    is_new = _remember_delivery(parsed.delivery_id)
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

    logger.info("received oauth callback code=<redacted len=%d> state=%s", len(code), state)
    return Response(
        content="caretaker: oauth callback received",
        media_type="text/plain",
    )


# Entrypoint for local testing:
# uvicorn src.caretaker.mcp_backend.main:app --reload
