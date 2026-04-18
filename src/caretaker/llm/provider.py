"""Multi-provider LLM abstraction.

Defines a minimal ``LLMProvider`` Protocol and two concrete implementations:

- ``AnthropicProvider`` — direct Anthropic SDK using ``AsyncAnthropic`` (truly async).
  Honors ``ANTHROPIC_API_KEY``.  Default provider to preserve legacy behavior.
- ``LiteLLMProvider`` — optional multi-provider backend via ``litellm.acompletion``.
  Supports Anthropic, OpenAI, Vertex, Azure OpenAI, Azure AI Foundry, Bedrock,
  Ollama, Mistral, Cohere, Groq and others through a single API.
  Requires ``pip install litellm`` and per-provider env vars:

  - Anthropic:         ``ANTHROPIC_API_KEY``
  - OpenAI:            ``OPENAI_API_KEY``
  - Azure OpenAI:      ``AZURE_API_KEY``, ``AZURE_API_BASE``, ``AZURE_API_VERSION``
                       (model string ``azure/<deployment>``)
  - Azure AI Foundry:  ``AZURE_AI_API_KEY``, ``AZURE_AI_API_BASE``
                       (model string ``azure_ai/<model>``)
  - Google Vertex:     ``VERTEX_PROJECT`` + ``GOOGLE_APPLICATION_CREDENTIALS``
  - AWS Bedrock:       ``AWS_ACCESS_KEY_ID`` + region
  - Ollama local:      ``OLLAMA_API_BASE``

Both providers fail *open*: if no credentials are configured, ``available`` is
False and ``complete()`` returns an empty response — callers already handle the
"claude not available" path by returning empty strings.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class LLMRequest:
    feature: str
    prompt: str
    model: str
    max_tokens: int
    temperature: float = 0.0


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal surface every backend must implement."""

    name: str

    @property
    def available(self) -> bool: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


# ── AnthropicProvider ─────────────────────────────────────────────────────────


class AnthropicProvider:
    """Direct Anthropic SDK backend with an async client.

    This replaces the previous sync ``anthropic.Anthropic`` usage that was
    blocking the event loop when called from ``async def`` methods.
    """

    name = "anthropic"

    def __init__(self, api_key: str | None = None, timeout: float = 60.0) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._timeout = timeout
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> None:
        if self._client is None and self._api_key:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, timeout=self._timeout)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.available:
            return LLMResponse(text="", model=request.model, provider=self.name)
        self._ensure_client()
        assert self._client is not None

        response = await self._client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            messages=[{"role": "user", "content": request.prompt}],
        )
        text = response.content[0].text if response.content else ""
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            model=request.model,
            provider=self.name,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )


# ── LiteLLMProvider ───────────────────────────────────────────────────────────


class LiteLLMProvider:
    """Multi-provider backend using LiteLLM.

    Args:
        fallback_models: Ordered list of model strings to try if the primary
            model fails (e.g. ``["openai/gpt-4o", "vertex_ai/gemini-1.5-pro"]``).
        timeout: Per-request timeout in seconds.
    """

    name = "litellm"

    def __init__(
        self,
        fallback_models: list[str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._fallback_models = list(fallback_models or [])
        self._timeout = timeout
        self._acompletion: Any = None
        self._import_error: Exception | None = None

        try:
            from litellm import acompletion  # type: ignore[import-not-found]

            self._acompletion = acompletion
        except ImportError as exc:
            self._import_error = exc
            logger.debug("LiteLLM not installed: %s", exc)

    @property
    def package_installed(self) -> bool:
        return self._acompletion is not None

    @property
    def available(self) -> bool:
        if self._acompletion is None:
            return False
        # At least one provider key must be present.  This mirrors the
        # behavior of LiteLLM itself (it will raise an auth error otherwise),
        # and lets callers fail-open when no credentials exist.
        return any(
            os.environ.get(key)
            for key in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "AZURE_API_KEY",
                "VERTEX_PROJECT",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_ACCESS_KEY_ID",
                "MISTRAL_API_KEY",
                "COHERE_API_KEY",
                "GROQ_API_KEY",
                "OLLAMA_API_BASE",
            )
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.available or self._acompletion is None:
            return LLMResponse(text="", model=request.model, provider=self.name)

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "timeout": self._timeout,
        }
        if self._fallback_models:
            kwargs["fallbacks"] = self._fallback_models

        response = await self._acompletion(**kwargs)

        choice = response.choices[0]
        text = choice.message.content or ""
        usage = getattr(response, "usage", None)
        actual_model = getattr(response, "model", request.model)
        cost = None
        try:
            from litellm import completion_cost  # type: ignore[import-not-found]

            cost = completion_cost(completion_response=response)
        except Exception:
            cost = None

        return LLMResponse(
            text=text,
            model=actual_model,
            provider=self.name,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cost_usd=cost,
        )


# ── NullProvider ──────────────────────────────────────────────────────────────


class NullProvider:
    """Always-unavailable provider.  Used when config explicitly disables LLM."""

    name = "null"
    available = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="", model=request.model, provider=self.name)


def build_provider(
    provider_name: str,
    *,
    timeout: float = 60.0,
    fallback_models: list[str] | None = None,
) -> LLMProvider:
    """Factory: construct a provider from its name.

    Unknown or explicitly disabled providers return a ``NullProvider``.
    """
    name = provider_name.lower().strip()
    if name == "anthropic":
        return AnthropicProvider(timeout=timeout)
    if name == "litellm":
        provider = LiteLLMProvider(fallback_models=fallback_models, timeout=timeout)
        if not provider.package_installed:
            logger.warning(
                "Configured provider 'litellm' but package not installed; "
                "falling back to AnthropicProvider"
            )
            return AnthropicProvider(timeout=timeout)
        return provider
    logger.warning("Unknown LLM provider '%s'; using NullProvider", provider_name)
    return NullProvider()
