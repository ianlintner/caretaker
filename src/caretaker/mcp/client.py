"""MCP Client scaffolding for caretaker."""

import logging
import os
from typing import Any

import httpx

from caretaker.config import MCPConfig

logger = logging.getLogger(__name__)


class MCPClient:
    """Lightweight client for communicating with the remote Azure MCP service."""

    def __init__(self, config: MCPConfig) -> None:
        if "*" in config.allowed_tools:
            raise ValueError(
                "Wildcard '*' is not permitted in allowed_tools; configure explicit tool names"
            )
        self.config = config
        self._session: httpx.AsyncClient | None = None
        self._connected = False

    def _auth_headers(self) -> dict[str, str]:
        mode = self.config.auth_mode

        if mode == "none":
            return {}

        if mode == "token":
            token = os.environ.get("CARETAKER_MCP_AUTH_TOKEN", "").strip()
            if not token:
                raise RuntimeError("CARETAKER_MCP_AUTH_TOKEN is required when mcp.auth_mode=token")
            return {"Authorization": f"Bearer {token}"}

        if mode in {"managed_identity", "apim"}:
            principal_id = os.environ.get("CARETAKER_MCP_CLIENT_PRINCIPAL_ID", "").strip()
            if not principal_id:
                raise RuntimeError(
                    f"CARETAKER_MCP_CLIENT_PRINCIPAL_ID is required when mcp.auth_mode={mode}"
                )
            return {"x-ms-client-principal-id": principal_id}

        raise RuntimeError(f"Unsupported mcp auth mode: {mode}")

    async def connect(self) -> None:
        """Initialize the connection to the remote MCP server."""
        if not self.config.enabled or not self.config.endpoint:
            logger.debug("MCP client is disabled or missing endpoint.")
            return

        logger.info("Connecting to remote MCP server at %s", self.config.endpoint)
        self._session = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        # Verify the endpoint is reachable before marking as connected
        if await self.is_healthy():
            self._connected = True
        else:
            logger.error("Failed to connect to MCP server at %s", self.config.endpoint)
            self._connected = False

    async def disconnect(self) -> None:
        """Close the connection to the remote MCP server."""
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        if self._connected:
            logger.info("Disconnecting from remote MCP server.")
            self._connected = False

    async def is_healthy(self) -> bool:
        """Check if the remote MCP service is reachable and healthy."""
        if not self.config.endpoint:
            return False
        if self._session is None:
            self._session = httpx.AsyncClient(timeout=self.config.timeout_seconds)

        url = f"{self.config.endpoint.rstrip('/')}/health"
        try:
            response = await self._session.get(url)
            if response.status_code != 200:
                logger.warning(
                    "MCP health check failed: %s %s",
                    response.status_code,
                    response.text,
                )
                return False
            payload = response.json()
            return bool(payload.get("status") == "ok")
        except Exception:
            logger.exception("MCP health check request failed for %s", url)
            return False

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool on the remote MCP server."""
        if not self._connected:
            raise RuntimeError("MCPClient is not connected")

        if "*" in self.config.allowed_tools:
            raise ValueError(
                "Wildcard '*' is not permitted in allowed_tools; configure explicit tool names"
            )

        if tool_name not in self.config.allowed_tools:
            allowed = ", ".join(self.config.allowed_tools)
            raise ValueError(f"Tool '{tool_name}' is not permitted. Allowed tools: {allowed}")
        logger.info("Calling remote tool %s", tool_name)
        if self._session is None or not self.config.endpoint:
            raise RuntimeError("MCP client session is not initialized")

        url = f"{self.config.endpoint.rstrip('/')}/mcp/tools/call"
        response = await self._session.post(
            url,
            json={"tool_name": tool_name, "arguments": arguments},
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return response.json()
