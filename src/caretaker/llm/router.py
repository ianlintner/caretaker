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
    "principal_architecture_review",
    "principal_create_prd",
    "principal_decompose_refactor",
    "test_coverage_analysis",
    "test_skeleton_generation",
    "refactor_analysis",
    "refactor_plan",
    "perf_diff_analysis",
    "migration_analysis",
    "migration_plan",
}


class LLMRouter:
    """Routes analysis tasks to the configured LLM backend.

    Public API preserved for callers: ``feature_enabled(feature)``,
    ``claude_available``, and the ``claude`` property remain stable.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._claude = ClaudeClient(config=config)

        if config.llm_enabled == "auto":
            self._active = self._claude.available
        elif config.llm_enabled == "true":
            self._active = self._claude.available
            if not self._active:
                logger.warning(
                    "LLM enabled in config but no provider credentials detected (provider=%s)",
                    config.provider,
                )
        else:
            # Hard-disabled by config (llm_enabled="false"). Warn loudly when
            # credentials for the selected provider *are* present — this is
            # almost always a misconfiguration: the legacy field name
            # ``claude_enabled`` makes it look like it only toggles Anthropic,
            # but it actually kills the whole router (LiteLLM / Azure AI /
            # OpenAI included). See ``docs/qa-findings-2026-04-23.md`` #1.
            self._active = False
            if self._claude.available:
                logger.warning(
                    "LLM router hard-disabled by config (llm_enabled='false') "
                    "but provider credentials are present (provider=%s). "
                    "All LLM features will fall back to their non-LLM paths. "
                    "If this is unintentional, set llm_enabled='auto'.",
                    config.provider,
                )

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
