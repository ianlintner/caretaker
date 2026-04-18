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

import json
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
    # Optional pre-built chat history used by the tool-use loop.  When set it
    # supersedes ``prompt``; ``complete`` callers continue to pass ``prompt``
    # only.
    messages: list[dict[str, Any]] | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass
class LLMToolCall:
    """A tool-call emitted by the model.

    ``id`` is the provider-assigned identifier that must be echoed back on the
    corresponding ``tool`` result message in the next request.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMToolResponse:
    """Response from a tool-use completion.

    Exactly one of ``text`` or ``tool_calls`` is expected to carry content:
    models either emit a final textual reply *or* ask for tool calls.
    """

    text: str
    tool_calls: list[LLMToolCall]
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    stop_reason: str | None = None
    # Raw assistant message as returned by the provider, suitable for
    # appending to the next request's ``messages`` list verbatim.
    raw_message: dict[str, Any] | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal surface every backend must implement."""

    name: str

    @property
    def available(self) -> bool: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def complete_with_tools(
        self,
        request: LLMRequest,
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        """Tool-use completion.

        ``tools`` uses the OpenAI function-calling JSON schema shape
        (``{"type": "function", "function": {"name", "description", "parameters"}}``).
        LiteLLM normalises this across providers.

        Providers that do not support tool-use should raise
        :class:`NotImplementedError`.
        """
        ...


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

    async def complete_with_tools(
        self,
        request: LLMRequest,
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        raise NotImplementedError(
            "AnthropicProvider does not implement tool-use; use provider='litellm' "
            "with an Anthropic or Azure model for tool-calling."
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
            from litellm import completion_cost

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

    async def complete_with_tools(
        self,
        request: LLMRequest,
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        """Tool-use completion via LiteLLM.

        Accepts an optional pre-built ``request.messages`` list; when absent
        it falls back to a single-user-turn message built from ``request.prompt``.
        """
        if not self.available or self._acompletion is None:
            raise RuntimeError("LiteLLM provider unavailable (package missing or no credentials)")

        messages: list[dict[str, Any]] = (
            list(request.messages)
            if request.messages is not None
            else [{"role": "user", "content": request.prompt}]
        )

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "timeout": self._timeout,
            "tools": tools,
            "tool_choice": "auto",
        }
        if self._fallback_models:
            kwargs["fallbacks"] = self._fallback_models

        response = await self._acompletion(**kwargs)
        choice = response.choices[0]
        message = choice.message
        text = getattr(message, "content", None) or ""
        finish_reason = getattr(choice, "finish_reason", None)

        raw_tool_calls = getattr(message, "tool_calls", None) or []
        tool_calls: list[LLMToolCall] = []
        for tc in raw_tool_calls:
            name = getattr(tc.function, "name", "") if getattr(tc, "function", None) else ""
            raw_args = (
                getattr(tc.function, "arguments", "") if getattr(tc, "function", None) else ""
            )
            try:
                parsed_args = json.loads(raw_args) if raw_args else {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {"_raw": parsed_args}
            except (TypeError, ValueError):
                parsed_args = {"_raw": raw_args}
            tool_calls.append(
                LLMToolCall(
                    id=getattr(tc, "id", "") or "",
                    name=name,
                    arguments=parsed_args,
                )
            )

        # Build a round-trip-safe raw_message so the caller can append it to
        # the next request's messages array verbatim. LiteLLM message objects
        # are Pydantic-ish; fall back to a manual dict build.
        raw_message: dict[str, Any]
        try:
            raw_message = message.model_dump()
        except AttributeError:
            raw_message = {
                "role": getattr(message, "role", "assistant"),
                "content": text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ]
                or None,
            }

        usage = getattr(response, "usage", None)
        actual_model = getattr(response, "model", request.model)
        cost = None
        try:
            from litellm import completion_cost

            cost = completion_cost(completion_response=response)
        except Exception:
            cost = None

        return LLMToolResponse(
            text=text,
            tool_calls=tool_calls,
            model=actual_model,
            provider=self.name,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cost_usd=cost,
            stop_reason=finish_reason,
            raw_message=raw_message,
        )


# ── NullProvider ──────────────────────────────────────────────────────────────


class NullProvider:
    """Always-unavailable provider.  Used when config explicitly disables LLM."""

    name = "null"
    available = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="", model=request.model, provider=self.name)

    async def complete_with_tools(
        self,
        request: LLMRequest,
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        raise NotImplementedError("NullProvider does not support tool-use")


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
