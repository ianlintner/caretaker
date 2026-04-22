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

import json
import logging
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ValidationError

from .provider import AnthropicProvider, LLMRequest, build_provider

if TYPE_CHECKING:
    from caretaker.config import LLMConfig

    from .provider import LLMProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredCompleteError(Exception):
    """Raised when ``ClaudeClient.structured_complete`` exhausts its retries.

    Attributes:
        raw_text: Last raw LLM response received, for diagnostic logging.
        validation_error: The underlying :class:`pydantic.ValidationError` or
            :class:`json.JSONDecodeError` that caused the final failure.
    """

    def __init__(
        self,
        raw_text: str,
        validation_error: Exception,
    ) -> None:
        super().__init__(
            f"LLM structured completion failed validation after retries: "
            f"{type(validation_error).__name__}: {validation_error}"
        )
        self.raw_text = raw_text
        self.validation_error = validation_error


def _strip_code_fences(text: str) -> str:
    """Strip optional ```json ... ``` fences some models emit despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # drop opening fence (possibly with language tag) and trailing fence
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
    return stripped


# Default per-feature model/max-tokens fallback when LLMConfig is not supplied
# (legacy callers that instantiate ``ClaudeClient()`` directly, e.g. tests).
_FALLBACK_MODEL = "claude-sonnet-4-5"
_FALLBACK_REASONING_MODEL = "claude-opus-4-5"
_LEGACY_FEATURE_DEFAULTS: dict[str, tuple[str, int]] = {
    "ci_log_analysis": (_FALLBACK_MODEL, 2000),
    "analyze_review_comment": (_FALLBACK_MODEL, 1000),
    "generate_reflection": (_FALLBACK_MODEL, 1500),
    "generate_recovery_plan": (_FALLBACK_MODEL, 2000),
    "analyze_stuck_pr": (_FALLBACK_MODEL, 800),
    "decompose_issue": (_FALLBACK_MODEL, 3000),
    "principal_architecture_review": (_FALLBACK_REASONING_MODEL, 4000),
    "principal_create_prd": (_FALLBACK_REASONING_MODEL, 6000),
    "principal_decompose_refactor": (_FALLBACK_REASONING_MODEL, 5000),
    "test_coverage_analysis": (_FALLBACK_REASONING_MODEL, 3000),
    "test_skeleton_generation": (_FALLBACK_REASONING_MODEL, 4000),
    "refactor_analysis": (_FALLBACK_REASONING_MODEL, 4000),
    "refactor_plan": (_FALLBACK_REASONING_MODEL, 3000),
    "perf_diff_analysis": (_FALLBACK_REASONING_MODEL, 3000),
    "migration_analysis": (_FALLBACK_REASONING_MODEL, 4000),
    "migration_plan": (_FALLBACK_REASONING_MODEL, 5000),
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

    async def complete(
        self,
        feature: str,
        prompt: str,
        max_tokens: int = 2000,
        *,
        system: str | None = None,
    ) -> str:
        """General-purpose completion method.

        Prepends an optional ``system`` instruction to the user prompt before
        calling the underlying LLM.  Intended for callers (e.g. pr_reviewer)
        that need a free-form system prompt rather than a domain-specific
        method from the public feature API below.
        """
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return await self._complete(feature, full_prompt, max_tokens)

    async def structured_complete(
        self,
        prompt: str,
        *,
        schema: type[T],
        feature: str = "structured_complete",
        system: str | None = None,
        max_retries: int | None = None,
        max_tokens: int = 2000,
        model: str | None = None,
    ) -> T:
        """Call the LLM and parse its response into ``schema``.

        The request's system prompt (or user prompt if no system is supplied)
        is prefixed with a terse instruction telling the model to emit a single
        JSON object matching ``schema.model_json_schema()``. The response is
        JSON-decoded and passed through ``schema.model_validate``. On
        :class:`json.JSONDecodeError` or :class:`pydantic.ValidationError`,
        the helper re-invokes the model once (by default) with the previous
        raw output and the validation error appended, asking it to self-correct.

        Args:
            prompt: The user-turn prompt.
            schema: The :class:`pydantic.BaseModel` subclass to validate against.
            feature: Feature key used for model resolution and logging.
                Defaults to ``"structured_complete"``.
            system: Optional system instruction; the schema prefix is prepended
                to it (or to ``prompt`` if ``system`` is ``None``).
            max_retries: Number of retries on parse/validation failure.
                Defaults to ``LLMConfig.structured_output_retries`` (1).
            max_tokens: Hard cap on output length.
            model: Optional explicit model string. When set, overrides
                the resolved per-feature model.

        Returns:
            An instance of ``schema``.

        Raises:
            StructuredCompleteError: When all attempts fail validation.
        """
        if max_retries is None:
            max_retries = self._config.structured_output_retries if self._config is not None else 1

        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        prefix = f"Respond with only a single JSON object matching this schema: {schema_json}"
        effective_system = f"{prefix}\n\n{system}" if system else prefix

        # Resolve model and max_tokens like ``complete`` does; allow override.
        resolved_model, resolved_max_tokens = self._resolve_feature(feature, max_tokens)
        if model is not None:
            resolved_model = model

        attempt_prompt = prompt
        last_text = ""
        last_error: Exception | None = None

        attempts = max_retries + 1
        for attempt in range(attempts):
            if not self.available:
                # No provider — surface as a validation failure so callers
                # don't get silently downgraded to an empty T.
                raise StructuredCompleteError(
                    raw_text="",
                    validation_error=RuntimeError("LLM provider unavailable"),
                )

            full_prompt = f"{effective_system}\n\n{attempt_prompt}"
            _log_prompt(feature, full_prompt)
            try:
                response = await self._provider.complete(
                    LLMRequest(
                        feature=feature,
                        prompt=full_prompt,
                        model=resolved_model,
                        max_tokens=resolved_max_tokens,
                    )
                )
            except Exception as exc:
                # Provider-level failure: surface to caller rather than swallowing.
                raise StructuredCompleteError(raw_text="", validation_error=exc) from exc

            _log_response(feature, response.text, response)
            last_text = response.text or ""
            cleaned = _strip_code_fences(last_text)

            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                last_error = exc
                logger.warning(
                    "structured_complete[%s] attempt %d: JSON decode failed: %s",
                    feature,
                    attempt + 1,
                    exc,
                )
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response failed to parse: {exc}. "
                    "Return only valid JSON matching the schema above."
                )
                continue

            try:
                return schema.model_validate(parsed)
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "structured_complete[%s] attempt %d: schema validation failed: %s",
                    feature,
                    attempt + 1,
                    exc,
                )
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response failed to parse: {exc}. "
                    "Return only valid JSON matching the schema above."
                )
                continue

        assert last_error is not None  # loop always sets it on failure
        raise StructuredCompleteError(raw_text=last_text, validation_error=last_error)

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
            f"Generate a recovery plan for a CRITICAL goal: '{goal_id}' "
            f"(score={goal_score:.2f}).\n\n"
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
            + (
                f"Previously successful strategies for similar failures:\n{known_skills}\n\n"
                if known_skills
                else ""
            )
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

    # ── Principal agent features ────────────────────────────────────────────

    async def analyze_architecture(self, diff: str, repo_context: str = "") -> str:
        """Review a PR diff for architectural patterns and consistency."""
        prompt = (
            "You are a principal engineer reviewing this PR for architectural quality.\n"
            "Evaluate:\n"
            "1. Consistency with existing patterns and conventions\n"
            "2. Separation of concerns and module boundaries\n"
            "3. API design quality (naming, signatures, contracts)\n"
            "4. Error handling and edge cases\n"
            "5. Potential scalability or maintainability issues\n\n"
            f"Repository context: {repo_context}\n\n"
            f"Diff:\n```\n{diff[:12000]}\n```\n\n"
            "Provide:\n"
            "VERDICT: APPROVE | REQUEST_CHANGES | COMMENT\n"
            "SUMMARY: one paragraph architectural assessment\n"
            "FINDINGS: numbered list of specific findings with severity (critical/major/minor)"
        )
        return await self._complete("principal_architecture_review", prompt, 4000)

    async def create_prd(self, issue_body: str, repo_context: str = "") -> str:
        """Generate a structured PRD from an issue or feature request."""
        prompt = (
            "You are a principal engineer creating a Product Requirements Document.\n"
            "Based on the following issue, produce a structured PRD.\n\n"
            f"Repository context: {repo_context}\n\n"
            f"Issue:\n{issue_body}\n\n"
            "PRD sections:\n"
            "## Overview\n"
            "## Goals\n"
            "## Non-Goals\n"
            "## Background & Motivation\n"
            "## Detailed Design\n"
            "## Milestones (ordered, PR-sized)\n"
            "## Acceptance Criteria\n"
            "## Open Questions\n"
            "## Risks & Mitigations"
        )
        return await self._complete("principal_create_prd", prompt, 6000)

    async def decompose_refactor(self, description: str, repo_context: str = "") -> str:
        """Plan a large refactor into ordered, PR-sized steps."""
        prompt = (
            "You are a principal engineer planning a large refactoring effort.\n"
            "Break this into ordered, independently-reviewable PR-sized steps.\n\n"
            f"Repository context: {repo_context}\n\n"
            f"Refactor description:\n{description}\n\n"
            "For each step provide:\n"
            "- Step number and title\n"
            "- Description of changes\n"
            "- Files affected\n"
            "- Dependencies on prior steps\n"
            "- Risk level (low/medium/high)\n"
            "- Estimated diff size (small/medium/large)\n"
            "- Rollback strategy"
        )
        return await self._complete("principal_decompose_refactor", prompt, 5000)

    # ── Test agent features ─────────────────────────────────────────────────

    async def analyze_test_coverage(self, diff: str, existing_tests: str = "") -> str:
        """Analyze a PR diff for missing test coverage."""
        prompt = (
            "Analyze this PR diff for test coverage gaps.\n"
            "Identify:\n"
            "1. New code paths that lack test coverage\n"
            "2. Edge cases not covered by existing tests\n"
            "3. Error/exception paths that should be tested\n"
            "4. Integration points that need testing\n\n"
            f"Existing test files:\n{existing_tests[:4000]}\n\n"
            f"PR Diff:\n```\n{diff[:8000]}\n```\n\n"
            "For each gap provide:\n"
            "- What to test\n"
            "- Suggested test name\n"
            "- Test type (unit/integration/e2e)\n"
            "- Priority (critical/important/nice-to-have)"
        )
        return await self._complete("test_coverage_analysis", prompt, 3000)

    async def generate_test_skeleton(self, code: str, context: str = "") -> str:
        """Generate test skeleton code for the given source code."""
        prompt = (
            "Generate test skeleton code for the following source code.\n"
            "Include:\n"
            "1. Test class/function structure with descriptive names\n"
            "2. Setup/teardown patterns as needed\n"
            "3. Mock definitions for external dependencies\n"
            "4. Test cases for happy path and key edge cases\n"
            "5. Assertions with clear expected values\n\n"
            f"Context: {context}\n\n"
            f"Source code:\n```\n{code[:8000]}\n```\n\n"
            "Output valid, runnable test code using pytest."
        )
        return await self._complete("test_skeleton_generation", prompt, 4000)

    # ── Refactor agent features ─────────────────────────────────────────────

    async def analyze_code_smells(self, code: str, context: str = "") -> str:
        """Analyze code for code smells and refactoring opportunities."""
        prompt = (
            "Analyze this code for code smells and refactoring opportunities.\n"
            "Check for:\n"
            "1. Dead code (unreachable or unused)\n"
            "2. Duplication (similar logic repeated)\n"
            "3. Long functions (>50 lines or too many responsibilities)\n"
            "4. Complex conditionals (deeply nested or overly broad)\n"
            "5. Poor naming or unclear abstractions\n"
            "6. Circular dependencies\n\n"
            f"Context: {context}\n\n"
            f"Code:\n```\n{code[:10000]}\n```\n\n"
            "For each smell provide:\n"
            "SMELL: <category>\n"
            "LOCATION: <file:line or function name>\n"
            "SEVERITY: critical | major | minor\n"
            "SUGGESTION: specific refactoring approach\n"
            "CONFIDENCE: 0.0-1.0"
        )
        return await self._complete("refactor_analysis", prompt, 4000)

    async def plan_refactor(self, smells: str, context: str = "") -> str:
        """Create a refactoring plan from identified code smells."""
        prompt = (
            "Create a refactoring plan to address these code smells.\n"
            "Group related smells and order steps for minimal risk.\n\n"
            f"Context: {context}\n\n"
            f"Identified smells:\n{smells}\n\n"
            "For each refactoring step provide:\n"
            "- Step number and title\n"
            "- Smells addressed\n"
            "- Specific changes to make\n"
            "- Files affected\n"
            "- Risk level\n"
            "- Verification approach"
        )
        return await self._complete("refactor_plan", prompt, 3000)

    # ── Performance agent features ──────────────────────────────────────────

    async def analyze_perf_diff(self, diff: str, context: str = "") -> str:
        """Analyze a PR diff for performance anti-patterns."""
        prompt = (
            "Analyze this PR diff for performance issues and anti-patterns.\n"
            "Check for:\n"
            "1. N+1 query patterns (loops making individual DB/API calls)\n"
            "2. Unbounded loops or missing pagination\n"
            "3. Large memory allocations or data structure copies\n"
            "4. Missing caching for expensive operations\n"
            "5. Blocking I/O in async contexts\n"
            "6. Unnecessary serialization/deserialization\n\n"
            f"Context: {context}\n\n"
            f"Diff:\n```\n{diff[:10000]}\n```\n\n"
            "For each issue provide:\n"
            "PATTERN: <anti-pattern name>\n"
            "LOCATION: <file:line>\n"
            "SEVERITY: critical | warning | info\n"
            "IMPACT: expected performance impact\n"
            "FIX: specific improvement suggestion"
        )
        return await self._complete("perf_diff_analysis", prompt, 3000)

    # ── Migration agent features ────────────────────────────────────────────

    async def analyze_migration(self, code: str, target: str = "") -> str:
        """Analyze code for deprecated API usage needing migration."""
        prompt = (
            "Analyze this code for deprecated or soon-to-be-deprecated API usage.\n\n"
            f"Migration target: {target}\n\n"
            f"Code:\n```\n{code[:10000]}\n```\n\n"
            "For each deprecated usage provide:\n"
            "DEPRECATED: <old API/pattern>\n"
            "REPLACEMENT: <new API/pattern>\n"
            "LOCATION: <file:line>\n"
            "COMPLEXITY: simple | moderate | complex\n"
            "AUTO_FIXABLE: yes | no\n"
            "NOTES: migration considerations"
        )
        return await self._complete("migration_analysis", prompt, 4000)

    async def plan_migration(self, deprecations: str, target: str = "") -> str:
        """Create an ordered migration plan from deprecated API findings."""
        prompt = (
            "Create an ordered migration plan for the following deprecated API usages.\n"
            "Group related changes and order for minimal breakage.\n\n"
            f"Migration target: {target}\n\n"
            f"Deprecated usages:\n{deprecations}\n\n"
            "For each migration step provide:\n"
            "- Step number and title\n"
            "- Deprecations addressed\n"
            "- Specific code changes\n"
            "- Files affected\n"
            "- Breaking change risk (yes/no)\n"
            "- Rollback strategy\n"
            "- Verification approach"
        )
        return await self._complete("migration_plan", prompt, 5000)


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
