"""Minimal backend service scaffolding for Caretaker MCP endpoints."""

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
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


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Basic health probe for Kubernetes or Container Apps."""
    return {"status": "ok", "version": "0.1.0"}


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

    # Optional auth/governance check can go here
    auth_mode = os.environ.get("CARETAKER_MCP_AUTH_MODE", "none")
    if auth_mode == "token":
        # Placeholder for token validation
        pass

    if req.tool_name == "example_tool":
        return ToolCallResponse(
            status="success",
            tool_name=req.tool_name,
            result={"message": f"Hello from example_tool with args: {req.arguments}"},
        )

    raise HTTPException(status_code=404, detail=f"Tool {req.tool_name} not found")


# Entrypoint for local testing:
# uvicorn src.caretaker.mcp_backend.main:app --reload
