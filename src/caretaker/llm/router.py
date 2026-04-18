"""LLM task router — directs analysis tasks to the configured provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .claude import ClaudeClient

if TYPE_CHECKING:
    from caretaker.config import LLMConfig

logger = logging.getLogger(__name__)

# Features that benefit from Claude's reasoning (historical name — now applies
# to whichever provider is configured).
CLAUDE_FEATURES = {
    "ci_log_analysis",
    "architectural_review",
    "issue_decomposition",
    "upgrade_impact_analysis",
}


class LLMRouter:
    """Routes analysis tasks to the configured LLM backend.

    Public API preserved for callers: ``feature_enabled(feature)``,
    ``claude_available``, and the ``claude`` property remain stable.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._claude = ClaudeClient(config=config)

        if config.claude_enabled == "auto":
            self._active = self._claude.available
        elif config.claude_enabled == "true":
            self._active = self._claude.available
            if not self._active:
                logger.warning(
                    "LLM enabled in config but no provider credentials detected (provider=%s)",
                    config.provider,
                )
        else:
            self._active = False

        if self._active:
            logger.info(
                "LLM integration active — provider=%s default_model=%s fallbacks=%d",
                config.provider,
                config.default_model,
                len(config.fallback_models),
            )
        else:
            logger.info("LLM not available — analysis features disabled")

    @property
    def claude_available(self) -> bool:
        """Retained name for backwards compatibility."""
        return self._active

    @property
    def available(self) -> bool:
        return self._active

    def feature_enabled(self, feature: str) -> bool:
        return self._active and feature in self._config.claude_features

    @property
    def claude(self) -> ClaudeClient:
        """Retained name for backwards compatibility."""
        return self._claude
