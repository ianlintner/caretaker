"""Minimal backend service scaffolding for Caretaker MCP endpoints."""

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Caretaker MCP Backend",
    description="Minimal backend service for remote Caretaker capabilities.",
    version="0.1.0",
)

# ── Models ─────────────────────────────────────────────────────────────


class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any]


class ToolCallResponse(BaseModel):
    status: str
    tool_name: str
    result: Any


# ── Endpoints ──────────────────────────────────────────────────────────


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
    return {"status": "ok", "version": "0.1.0"}


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


# Entrypoint for local testing:
# uvicorn src.caretaker.mcp_backend.main:app --reload
