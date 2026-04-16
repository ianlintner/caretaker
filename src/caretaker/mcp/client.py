"""MCP Client scaffolding for caretaker."""

import logging
from typing import Any

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
        self._session: object | None = None  # Placeholder for an async HTTP/MCP session
        self._connected = False

    async def connect(self) -> None:
        """Initialize the connection to the remote MCP server."""
        if not self.config.enabled or not self.config.endpoint:
            logger.debug("MCP client is disabled or missing endpoint.")
            return

        logger.info(f"Connecting to remote MCP server at {self.config.endpoint}")
        # Verify the endpoint is reachable before marking as connected
        if await self.is_healthy():
            self._connected = True
        else:
            logger.error("Failed to connect to MCP server at %s", self.config.endpoint)
            self._connected = False

    async def disconnect(self) -> None:
        """Close the connection to the remote MCP server."""
        if self._connected:
            logger.info("Disconnecting from remote MCP server.")
            self._connected = False

    async def is_healthy(self) -> bool:
        """Check if the remote MCP service is reachable and healthy."""
        # Placeholder for actual health check. This must not depend on
        # ``self._connected`` because ``connect()`` calls it before the
        # client is marked connected.
        return True

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
        # Placeholder for actual tool invocation
        return {"status": "mock_success", "tool": tool_name}
