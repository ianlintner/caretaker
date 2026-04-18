"""LLM client for analysis features.

Historically named ``ClaudeClient`` and backed by the Anthropic SDK directly.
Now delegates to a pluggable ``LLMProvider`` (``AnthropicProvider`` by default,
or ``LiteLLMProvider`` for multi-provider routing: OpenAI, Vertex, Azure
OpenAI, Azure AI Foundry, Bedrock, Ollama, Mistral, Cohere, Groq, etc.).

The public method signatures are unchanged, so existing callers in
``pr_agent``, ``evolution/reflection``, ``evolution/planner`` and
``orchestrator`` keep working.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .provider import AnthropicProvider, LLMRequest, build_provider

if TYPE_CHECKING:
    from caretaker.config import LLMConfig

    from .provider import LLMProvider

logger = logging.getLogger(__name__)


# Default per-feature model/max-tokens fallback when LLMConfig is not supplied
# (legacy callers that instantiate ``ClaudeClient()`` directly, e.g. tests).
_FALLBACK_MODEL = "claude-sonnet-4-5"
_LEGACY_FEATURE_DEFAULTS: dict[str, tuple[str, int]] = {
    "ci_log_analysis":        (_FALLBACK_MODEL, 2000),
    "analyze_review_comment": (_FALLBACK_MODEL, 1000),
    "generate_reflection":    (_FALLBACK_MODEL, 1500),
    "generate_recovery_plan": (_FALLBACK_MODEL, 2000),
    "analyze_stuck_pr":       (_FALLBACK_MODEL, 800),
    "decompose_issue":        (_FALLBACK_MODEL, 3000),
}


class ClaudeClient:
    """LLM client with feature-scoped convenience methods.

    Retained name for backwards-compatibility.  Under the hood it routes all
    completion calls through a pluggable :class:`LLMProvider`.

    Args:
        api_key: Kept for backwards compatibility — forwarded to the default
            Anthropic provider when no ``config`` is supplied.
        config: If provided, drives provider selection, default model,
            per-feature overrides and fallbacks.
        provider: Optional pre-built provider.  Overrides ``config``/``api_key``.
            Primarily used for testing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        config: LLMConfig | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        self._config = config
        if provider is not None:
            self._provider: LLMProvider = provider
        elif config is not None:
            self._provider = build_provider(
                config.provider,
                timeout=config.timeout_seconds,
                fallback_models=list(config.fallback_models),
            )
        else:
            self._provider = AnthropicProvider(api_key=api_key)

    @property
    def available(self) -> bool:
        return self._provider.available

    @property
    def provider_name(self) -> str:
        return self._provider.name

    # ── Internal routing ─────────────────────────────────────────────────────

    def _resolve_feature(self, feature: str, default_max_tokens: int) -> tuple[str, int]:
        """Return the (model, max_tokens) pair for a feature."""
        if self._config is None:
            return _LEGACY_FEATURE_DEFAULTS.get(feature, (_FALLBACK_MODEL, default_max_tokens))

        from caretaker.config import DEFAULT_FEATURE_MODELS

        base = DEFAULT_FEATURE_MODELS.get(feature, {})
        override = self._config.feature_models.get(feature)

        model: str = str(base.get("model") or self._config.default_model)
        max_tokens = int(base.get("max_tokens") or default_max_tokens)
        if override is not None:
            if override.model:
                model = override.model
            if override.max_tokens:
                max_tokens = override.max_tokens
        return model, max_tokens

    async def _complete(self, feature: str, prompt: str, default_max_tokens: int) -> str:
        if not self.available:
            return ""
        model, max_tokens = self._resolve_feature(feature, default_max_tokens)

        _log_prompt(feature, prompt)
        try:
            response = await self._provider.complete(
                LLMRequest(feature=feature, prompt=prompt, model=model, max_tokens=max_tokens)
            )
        except Exception as exc:  # provider-level failures are non-fatal
            logger.warning("LLM call failed [%s]: %s", feature, exc)
            return ""

        _log_response(feature, response.text, response)
        return response.text

    # ── Public feature API ──────────────────────────────────────────────────

    async def analyze_ci_logs(self, logs: str, context: str = "") -> str:
        """Analyze CI failure logs and return structured diagnosis."""
        prompt = (
            "Analyze this CI failure log and provide:\n"
            "1. Root cause (one line)\n"
            "2. Affected files and lines\n"
            "3. Suggested fix (specific code changes)\n\n"
            f"Context: {context}\n\n"
            f"CI Log:\n```\n{logs[:8000]}\n```"
        )
        return await self._complete("ci_log_analysis", prompt, 2000)

    async def analyze_review_comment(self, comment: str, diff: str) -> str:
        """Classify a review comment as actionable / nitpick / question / praise."""
        prompt = (
            "Classify this code review comment:\n\n"
            f"Comment: {comment}\n\n"
            f"Diff context:\n```\n{diff[:4000]}\n```\n\n"
            "Respond with:\n"
            "CLASSIFICATION: ACTIONABLE | NITPICK | QUESTION | PRAISE\n"
            "SUMMARY: one-line description of what's needed\n"
            "COMPLEXITY: trivial | moderate | complex"
        )
        return await self._complete("analyze_review_comment", prompt, 1000)

    async def generate_reflection(self, prompt: str) -> str:
        """Generate a reflection analysis for stuck/diverging goals."""
        return await self._complete("generate_reflection", prompt, 1500)

    async def generate_recovery_plan(
        self,
        goal_id: str,
        goal_score: float,
        failing_context: str,
        known_skills: str = "",
    ) -> str:
        """Generate a step-by-step recovery plan for a CRITICAL goal."""
        prompt = (
            f"Generate a recovery plan for a CRITICAL goal: '{goal_id}' (score={goal_score:.2f}).\n\n"
            f"Current situation:\n{failing_context}\n\n"
            + (f"Known effective skills:\n{known_skills}\n\n" if known_skills else "")
            + "Provide a numbered list of 3-8 specific, actionable steps to recover this goal.\n"
            "Each step should be executable by a GitHub Copilot agent.\n"
            "Format: STEP N: <title> — <detailed instructions>"
        )
        return await self._complete("generate_recovery_plan", prompt, 2000)

    async def analyze_stuck_pr(
        self,
        pr_number: int,
        previous_attempts: int,
        ci_log: str,
        known_skills: str = "",
    ) -> str:
        """Analyze a PR that has been stuck in CI_FAILING for multiple cycles."""
        prompt = (
            f"PR #{pr_number} has failed CI {previous_attempts} time(s).\n\n"
            f"Latest CI log:\n```\n{ci_log[:6000]}\n```\n\n"
            + (f"Previously successful strategies for similar failures:\n{known_skills}\n\n" if known_skills else "")
            + "Provide a focused analysis:\n"
            "1. Why previous fix attempts likely failed\n"
            "2. The most likely root cause given the current log\n"
            "3. A specific, different approach to try next\n"
            "Keep response under 300 words."
        )
        return await self._complete("analyze_stuck_pr", prompt, 800)

    async def decompose_issue(self, issue_body: str, repo_context: str = "") -> str:
        """Break a large issue into smaller implementable tasks."""
        prompt = (
            "Break this issue into smaller, implementable sub-issues.\n"
            "Each should be a focused PR-sized task.\n\n"
            f"Repository context: {repo_context}\n\n"
            f"Issue:\n{issue_body}\n\n"
            "For each sub-issue provide:\n"
            "- Title\n"
            "- Description\n"
            "- Acceptance criteria\n"
            "- Files likely involved\n"
            "- Dependencies on other sub-issues"
        )
        return await self._complete("decompose_issue", prompt, 3000)


# ── Logging helpers ──────────────────────────────────────────────────────────


def _log_prompt(feature: str, prompt: str) -> None:
    preview = prompt[:2000] + "…" if len(prompt) > 2000 else prompt
    logger.debug("LLM request [%s] prompt:\n%s", feature, preview)


def _log_response(feature: str, text: str, response: object) -> None:
    preview = text[:2000] + "…" if len(text) > 2000 else text
    input_tokens = getattr(response, "input_tokens", 0)
    output_tokens = getattr(response, "output_tokens", 0)
    cost = getattr(response, "cost_usd", None)
    model = getattr(response, "model", "?")
    provider = getattr(response, "provider", "?")
    cost_str = f" cost=${cost:.4f}" if cost else ""
    logger.debug(
        "LLM response [%s] provider=%s model=%s tokens=%d/%d%s:\n%s",
        feature,
        provider,
        model,
        input_tokens,
        output_tokens,
        cost_str,
        preview,
    )
