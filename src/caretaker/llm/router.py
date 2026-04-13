"""LLM task router — directs tasks to Copilot or Claude."""

from __future__ import annotations

import logging

from caretaker.config import LLMConfig

from .claude import ClaudeClient

logger = logging.getLogger(__name__)

# Features that benefit from Claude's reasoning
CLAUDE_FEATURES = {
    "ci_log_analysis",
    "architectural_review",
    "issue_decomposition",
    "upgrade_impact_analysis",
}


class LLMRouter:
    """Routes analysis tasks to the appropriate LLM backend."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._claude = ClaudeClient()

        # Determine Claude availability
        if config.claude_enabled == "auto":
            self._claude_active = self._claude.available
        elif config.claude_enabled == "true":
            self._claude_active = self._claude.available
            if not self._claude_active:
                logger.warning("Claude enabled in config but ANTHROPIC_API_KEY not set")
        else:
            self._claude_active = False

        if self._claude_active:
            logger.info("Claude integration active — premium features enabled")
        else:
            logger.info("Claude not available — using Copilot-only mode")

    @property
    def claude_available(self) -> bool:
        return self._claude_active

    def feature_enabled(self, feature: str) -> bool:
        return self._claude_active and feature in self._config.claude_features

    @property
    def claude(self) -> ClaudeClient:
        return self._claude
