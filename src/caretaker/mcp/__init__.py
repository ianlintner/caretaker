"""Optional remote MCP client and telemetry abstractions."""

from .client import MCPClient
from .telemetry import TelemetryClient

__all__ = ["MCPClient", "TelemetryClient"]
