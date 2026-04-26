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

from caretaker.observability.metrics import record_llm_cache_usage

logger = logging.getLogger(__name__)


# Models that support Anthropic prompt caching via the ``cache_control`` content-
# block annotation.  We key off substring matches because both the Anthropic
# native SDK (``claude-*``) and LiteLLM-routed variants (``anthropic/claude-*``,
# ``vertex_ai/claude-*``, ``bedrock/anthropic.claude-*``) carry ``claude`` in
# the model id.
_CACHE_CAPABLE_MODEL_MARKERS: tuple[str, ...] = ("claude",)


def _supports_prompt_cache(model: str) -> bool:
    """Return True when ``model`` accepts Anthropic ``cache_control`` markers.

    Fail-open: unknown models return False so we never poison a request that
    the target provider would reject.  Keep the check cheap and substring-
    based — LiteLLM routes Claude through many namespaces.
    """
    lowered = model.lower()
    return any(marker in lowered for marker in _CACHE_CAPABLE_MODEL_MARKERS)


@dataclass
class LLMRequest:
    """A single completion request.

    ``messages`` is **only consumed by** :meth:`LLMProvider.complete_with_tools`
    — the non-tool-use :meth:`complete` path ignores it and always sends a
    single-user-turn message built from :attr:`prompt`. Callers that need
    multi-turn history must route through ``complete_with_tools``.
    """

    feature: str
    prompt: str
    model: str
    max_tokens: int
    temperature: float = 0.0
    # Optional pre-built chat history. When set, :meth:`complete_with_tools`
    # uses it verbatim instead of constructing a message from ``prompt``.
    # Ignored by :meth:`complete`.
    messages: list[dict[str, Any]] | None = None
    # Optional system prompt.  Used by :meth:`complete` to send a real
    # ``system`` parameter (enabling Anthropic prompt caching); ignored by
    # :meth:`complete_with_tools` which expects the system turn to already
    # live at the head of ``messages``.
    system: str | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    # Anthropic prompt-cache token counts (0 when the provider/model does not
    # surface them). ``cache_read_input_tokens`` is a cache *hit*;
    # ``cache_creation_input_tokens`` is the one-time write on a miss.
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


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
    # Anthropic prompt-cache token counts — see :class:`LLMResponse`.
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


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

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        # Send the system prompt as a structured text block carrying a
        # ``cache_control: ephemeral`` breakpoint. Anthropic caches the
        # tools → system → messages prefix up to the annotated block, so the
        # shared system prompt is hot on every subsequent call within the
        # 5-minute TTL. See ``shared/prompt-caching.md`` from the claude-api
        # skill for placement rationale.
        if request.system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        response = await self._client.messages.create(**kwargs)
        text = response.content[0].text if response.content else ""
        usage = getattr(response, "usage", None)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        record_llm_cache_usage(
            provider=self.name,
            model=request.model,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )
        return LLMResponse(
            text=text,
            model=request.model,
            provider=self.name,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
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
            from litellm import acompletion

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
                "AZURE_AI_API_KEY",
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

        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append(
                _make_system_message(
                    request.system,
                    cache=_supports_prompt_cache(request.model),
                )
            )
        messages.append({"role": "user", "content": request.prompt})

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
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

        cache_read, cache_creation = _extract_litellm_cache_tokens(usage)
        record_llm_cache_usage(
            provider=self.name,
            model=actual_model,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )

        return LLMResponse(
            text=text,
            model=actual_model,
            provider=self.name,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=cost,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
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
        if _supports_prompt_cache(request.model):
            messages = _annotate_system_cache_control(messages)

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

        cache_read, cache_creation = _extract_litellm_cache_tokens(usage)
        record_llm_cache_usage(
            provider=self.name,
            model=actual_model,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )

        return LLMToolResponse(
            text=text,
            tool_calls=tool_calls,
            model=actual_model,
            provider=self.name,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=cost,
            stop_reason=finish_reason,
            raw_message=raw_message,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        )


# ── Prompt-cache helpers ──────────────────────────────────────────────────────


def _make_system_message(system_text: str, *, cache: bool) -> dict[str, Any]:
    """Build a ``role: system`` message, optionally with an ephemeral cache marker.

    When ``cache`` is True, the content is rendered as a single ``text`` block
    carrying ``cache_control: {"type": "ephemeral"}``.  LiteLLM passes this
    through to Anthropic-family providers verbatim; OpenAI-family providers
    ignore the marker, so this is safe to send unconditionally as long as the
    model is cache-capable (see :func:`_supports_prompt_cache`).
    """
    if not cache:
        return {"role": "system", "content": system_text}
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def _annotate_system_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``messages`` with the leading system turn annotated for caching.

    The Foundry tool loop constructs a stable system prompt that is identical
    across every iteration of the loop — an ideal candidate for the Anthropic
    prompt cache's 5-minute ephemeral TTL.  We rewrite the *first* ``system``
    message in place so the rest of the ``messages`` list (tool results, user
    turns, assistant turns) is untouched.

    If ``messages`` is empty, has no leading system turn, or the system turn
    is already using a list-of-blocks content shape (assumed to be caller-
    configured), we return the list unchanged.
    """
    if not messages:
        return messages
    head = messages[0]
    if head.get("role") != "system":
        return messages
    content = head.get("content")
    if not isinstance(content, str) or not content:
        # Already structured (or empty) — leave it alone.
        return messages
    annotated: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        *messages[1:],
    ]
    return annotated


def _extract_litellm_cache_tokens(usage: Any) -> tuple[int, int]:
    """Pull Anthropic prompt-cache token counts from a LiteLLM ``usage`` object.

    LiteLLM surfaces cache telemetry under a few shapes depending on version
    and provider:

    - ``usage.cache_read_input_tokens`` / ``usage.cache_creation_input_tokens``
      (the Anthropic native keys, mirrored verbatim).
    - ``usage.prompt_tokens_details.cached_tokens`` (OpenAI-compatible
      aggregate, used as a fallback for read counts).

    Any missing/None attribute defaults to 0.  Returned as ``(read, creation)``.
    """
    if usage is None:
        return (0, 0)
    read = getattr(usage, "cache_read_input_tokens", None)
    if read is None:
        details = getattr(usage, "prompt_tokens_details", None)
        read = getattr(details, "cached_tokens", None) if details is not None else None
    creation = getattr(usage, "cache_creation_input_tokens", None)
    return (int(read or 0), int(creation or 0))


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
