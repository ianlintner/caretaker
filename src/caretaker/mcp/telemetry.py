"""Telemetry abstractions for Azure Application Insights."""

import logging
from typing import Any

from caretaker.config import TelemetryConfig

logger = logging.getLogger(__name__)


class TelemetryClient:
    """Client for pushing telemetry to Azure Monitor / App Insights."""

    def __init__(self, config: TelemetryConfig) -> None:
        self.config = config
        self._enabled = config.enabled

    def track_event(self, name: str, properties: dict[str, Any] | None = None) -> None:
        """Track a custom event."""
        if not self._enabled:
            return
        logger.debug(f"[Telemetry] Event: {name}", extra={"telemetry_props": properties})

    def track_metric(
        self, name: str, value: float, properties: dict[str, Any] | None = None
    ) -> None:
        """Track a custom metric."""
        if not self._enabled:
            return
        logger.debug(
            "[Telemetry] Metric: %s = %s", name, value, extra={"telemetry_props": properties}
        )

    def track_dependency(self, name: str, target: str, success: bool, duration_ms: float) -> None:
        """Track an external dependency call (like an MCP tool call)."""
        if not self._enabled:
            return
        logger.debug(
            f"[Telemetry] Dependency: {name} to {target} "
            f"(success={success}, duration={duration_ms}ms)"
        )
