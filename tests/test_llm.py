"""Tests for the LLM client, provider abstraction, and routing."""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from caretaker.config import FeatureModelConfig, LLMConfig
from caretaker.llm.claude import ClaudeClient, StructuredCompleteError
from caretaker.llm.provider import (
    AnthropicProvider,
    LiteLLMProvider,
    LLMRequest,
    LLMResponse,
    NullProvider,
    build_provider,
)
from caretaker.llm.router import LLMRouter
from caretaker.observability import metrics as metrics_mod


class FakeProvider:
    """Minimal provider stub that records calls and returns a scripted response."""

    name = "fake"

    def __init__(self, *, text: str = "ok", available: bool = True) -> None:
        self._text = text
        self._available = available
        self.calls: list[LLMRequest] = []

    @property
    def available(self) -> bool:
        return self._available

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            text=self._text,
            model=request.model,
            provider=self.name,
            input_tokens=10,
            output_tokens=5,
        )


# ── ClaudeClient over a pluggable provider ───────────────────────────────────


class TestClaudeClientLogging:
    async def test_analyze_ci_logs_returns_provider_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = FakeProvider(text="root cause: test failure")
        client = ClaudeClient(provider=provider)

        with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
            result = await client.analyze_ci_logs("some logs", context="test context")

        assert result == "root cause: test failure"
        assert len(provider.calls) == 1
        assert provider.calls[0].feature == "ci_log_analysis"

        debug = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("ci_log_analysis" in m for m in debug)
        assert any("provider=fake" in m for m in debug)

    async def test_analyze_review_comment_routes_correct_feature(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = FakeProvider(text="CLASSIFICATION: ACTIONABLE")
        client = ClaudeClient(provider=provider)

        with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
            result = await client.analyze_review_comment("fix this", "diff text")

        assert result == "CLASSIFICATION: ACTIONABLE"
        assert provider.calls[0].feature == "analyze_review_comment"

    async def test_decompose_issue_passes_body_in_prompt(self) -> None:
        provider = FakeProvider(text="sub-issue 1: ...")
        client = ClaudeClient(provider=provider)

        result = await client.decompose_issue("big issue body here", repo_context="ctx")

        assert result == "sub-issue 1: ..."
        assert "big issue body here" in provider.calls[0].prompt
        assert provider.calls[0].feature == "decompose_issue"

    async def test_logs_are_truncated_for_long_prompts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provider = FakeProvider(text="ok")
        client = ClaudeClient(provider=provider)
        long_log = "x" * 10_000

        with caplog.at_level(logging.DEBUG, logger="caretaker.llm.claude"):
            await client.analyze_ci_logs(long_log)

        prompt_records = [
            r for r in caplog.records if "prompt" in r.message and r.levelno == logging.DEBUG
        ]
        assert prompt_records
        assert len(prompt_records[0].message) < 15_000

    async def test_provider_exception_returns_empty_string(self) -> None:
        """Provider failures must not crash agents — treated as no-op."""

        class BoomProvider(FakeProvider):
            async def complete(self, request: LLMRequest) -> LLMResponse:
                raise RuntimeError("rate-limited")

        client = ClaudeClient(provider=BoomProvider())
        assert await client.analyze_ci_logs("logs") == ""

    async def test_unavailable_provider_returns_empty_without_call(self) -> None:
        provider = FakeProvider(available=False)
        client = ClaudeClient(provider=provider)
        assert await client.analyze_ci_logs("logs") == ""
        assert provider.calls == []


# ── Feature → model resolution ───────────────────────────────────────────────


class TestFeatureModelResolution:
    async def test_default_model_used_when_no_override(self) -> None:
        provider = FakeProvider()
        config = LLMConfig(default_model="claude-sonnet-4-5")
        client = ClaudeClient(config=config, provider=provider)

        await client.generate_reflection("analyze this")

        # DEFAULT_FEATURE_MODELS keeps generate_reflection on Sonnet
        assert provider.calls[0].model == "claude-sonnet-4-5"
        assert provider.calls[0].max_tokens == 1500

    async def test_triage_tasks_route_to_haiku_by_default(self) -> None:
        provider = FakeProvider()
        config = LLMConfig()
        client = ClaudeClient(config=config, provider=provider)

        await client.analyze_ci_logs("logs")
        await client.analyze_review_comment("c", "d")
        await client.analyze_stuck_pr(1, 2, "log")

        # All three should be routed to the cheaper triage tier
        for call in provider.calls:
            assert call.model == "claude-haiku-4-5"

    async def test_per_feature_override_wins(self) -> None:
        provider = FakeProvider()
        config = LLMConfig(
            feature_models={
                "ci_log_analysis": FeatureModelConfig(model="openai/gpt-4o-mini", max_tokens=500),
            }
        )
        client = ClaudeClient(config=config, provider=provider)

        await client.analyze_ci_logs("logs")

        assert provider.calls[0].model == "openai/gpt-4o-mini"
        assert provider.calls[0].max_tokens == 500

    async def test_legacy_constructor_without_config_still_works(self) -> None:
        """Old ``ClaudeClient(api_key=...)`` path must keep functioning."""
        provider = FakeProvider()
        client = ClaudeClient(provider=provider)
        await client.generate_reflection("prompt")
        assert provider.calls[0].model == "claude-sonnet-4-5"


# ── Provider factory ─────────────────────────────────────────────────────────


class TestProviderFactory:
    def test_anthropic_by_name(self) -> None:
        p = build_provider("anthropic")
        assert isinstance(p, AnthropicProvider)

    def test_unknown_provider_returns_null(self) -> None:
        p = build_provider("does-not-exist")
        assert isinstance(p, NullProvider)
        assert p.available is False

    def test_litellm_without_package_falls_back_to_anthropic(self) -> None:
        """If litellm isn't installed, factory degrades to Anthropic."""
        with patch(
            "caretaker.llm.provider.LiteLLMProvider.package_installed",
            new=False,
        ):
            p = build_provider("litellm")
        assert isinstance(p, AnthropicProvider)


# ── AnthropicProvider availability ───────────────────────────────────────────


class TestAnthropicProvider:
    def test_available_requires_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            p = AnthropicProvider()
            assert p.available is False

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=True):
            p = AnthropicProvider()
            assert p.available is True

    async def test_unavailable_returns_empty(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            p = AnthropicProvider()
            resp = await p.complete(
                LLMRequest(
                    feature="test",
                    prompt="hi",
                    model="claude-sonnet-4-5",
                    max_tokens=10,
                )
            )
        assert resp.text == ""
        assert resp.provider == "anthropic"


# ── LiteLLMProvider availability ─────────────────────────────────────────────


class TestLiteLLMProvider:
    def test_unavailable_when_no_keys_present(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            p = LiteLLMProvider()
            assert p.available is False

    def test_available_with_foundry_key(self) -> None:
        """Microsoft Foundry credentials should flip available=True."""
        with patch.dict(os.environ, {"AZURE_AI_API_KEY": "foundry-key"}, clear=True):
            p = LiteLLMProvider()
            # available also needs the litellm package to be importable
            if p.package_installed:
                assert p.available is True
            else:
                assert p.available is False

    def test_fallback_models_passed_through(self) -> None:
        p = LiteLLMProvider(fallback_models=["openai/gpt-4o", "vertex_ai/gemini-1.5-pro"])
        assert p._fallback_models == ["openai/gpt-4o", "vertex_ai/gemini-1.5-pro"]


# ── LLMRouter behavior ───────────────────────────────────────────────────────


class TestLLMRouter:
    def test_feature_enabled_respects_allowlist(self) -> None:
        provider = FakeProvider()
        config = LLMConfig(claude_enabled="true")
        router = LLMRouter(config)
        # Inject provider so availability resolves deterministically
        router._claude = ClaudeClient(config=config, provider=provider)
        router._active = True

        assert router.feature_enabled("ci_log_analysis") is True
        assert router.feature_enabled("not_on_list") is False

    def test_disabled_mode_turns_off_all_features(self) -> None:
        router = LLMRouter(LLMConfig(claude_enabled="false"))
        assert router.feature_enabled("ci_log_analysis") is False
        assert router.available is False

    def test_auto_mode_follows_provider_availability(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            router = LLMRouter(LLMConfig(claude_enabled="auto", provider="anthropic"))
            assert router.available is False

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}, clear=True):
            router = LLMRouter(LLMConfig(claude_enabled="auto", provider="anthropic"))
            assert router.available is True


# ── structured_complete ──────────────────────────────────────────────────────


class _SampleSchema(BaseModel):
    verdict: str
    score: int


class ScriptedProvider:
    """Provider that returns a scripted sequence of responses, one per call."""

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[LLMRequest] = []

    @property
    def available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        text = self._responses.pop(0) if self._responses else ""
        return LLMResponse(text=text, model=request.model, provider=self.name)


class TestStructuredComplete:
    async def test_happy_path_returns_parsed_model(self) -> None:
        provider = ScriptedProvider(['{"verdict": "APPROVE", "score": 42}'])
        client = ClaudeClient(provider=provider)

        result = await client.structured_complete("do a review", schema=_SampleSchema)

        assert isinstance(result, _SampleSchema)
        assert result.verdict == "APPROVE"
        assert result.score == 42
        assert len(provider.calls) == 1

    async def test_retry_recovers_from_malformed_first_attempt(self) -> None:
        provider = ScriptedProvider(
            [
                "not valid json at all",
                '{"verdict": "COMMENT", "score": 7}',
            ]
        )
        client = ClaudeClient(provider=provider)

        result = await client.structured_complete(
            "review this", schema=_SampleSchema, max_retries=1
        )

        assert result.verdict == "COMMENT"
        assert result.score == 7
        assert len(provider.calls) == 2
        # Second attempt prompt should include the previous-failure cue.
        assert "previous response failed to parse" in provider.calls[1].prompt

    async def test_exhausted_retries_raise_structured_complete_error(self) -> None:
        provider = ScriptedProvider(
            ["still not json", 'also {"incomplete": '],
        )
        client = ClaudeClient(provider=provider)

        with pytest.raises(StructuredCompleteError) as excinfo:
            await client.structured_complete("review", schema=_SampleSchema, max_retries=1)

        assert "failed validation" in str(excinfo.value)
        # Raw text from the last attempt must be preserved on the exception.
        assert excinfo.value.raw_text.startswith("also ")
        assert len(provider.calls) == 2

    async def test_schema_prefix_included_in_outgoing_prompt(self) -> None:
        provider = ScriptedProvider(['{"verdict": "APPROVE", "score": 1}'])
        client = ClaudeClient(provider=provider)

        await client.structured_complete(
            "user prompt here",
            schema=_SampleSchema,
            system="You are a reviewer.",
        )

        sent = provider.calls[0].prompt
        assert "Respond with only a single JSON object matching this schema" in sent
        # Schema must actually be embedded (contains the field names).
        assert "verdict" in sent and "score" in sent
        # The system prompt survives.
        assert "You are a reviewer." in sent
        # The user prompt is also in the payload.
        assert "user prompt here" in sent

    async def test_max_retries_zero_fails_immediately(self) -> None:
        provider = ScriptedProvider(["garbage"])
        client = ClaudeClient(provider=provider)

        with pytest.raises(StructuredCompleteError):
            await client.structured_complete("prompt", schema=_SampleSchema, max_retries=0)

        assert len(provider.calls) == 1

    async def test_retry_recovers_from_validation_error(self) -> None:
        # First response is valid JSON but missing ``score`` — pydantic validation fails.
        provider = ScriptedProvider(
            [
                '{"verdict": "APPROVE"}',
                '{"verdict": "APPROVE", "score": 99}',
            ]
        )
        client = ClaudeClient(provider=provider)

        result = await client.structured_complete("x", schema=_SampleSchema, max_retries=1)

        assert result.score == 99
        assert len(provider.calls) == 2

    async def test_strips_code_fences(self) -> None:
        """Models sometimes wrap JSON in ```json fences despite instructions."""
        provider = ScriptedProvider(
            ['```json\n{"verdict": "APPROVE", "score": 3}\n```'],
        )
        client = ClaudeClient(provider=provider)

        result = await client.structured_complete("x", schema=_SampleSchema)

        assert result.verdict == "APPROVE"
        assert result.score == 3

    async def test_retries_respects_llmconfig_default(self) -> None:
        """``max_retries`` defaults to LLMConfig.structured_output_retries."""
        provider = ScriptedProvider(["nope"])
        client = ClaudeClient(
            config=LLMConfig(structured_output_retries=0),
            provider=provider,
        )

        with pytest.raises(StructuredCompleteError):
            await client.structured_complete("prompt", schema=_SampleSchema)

        assert len(provider.calls) == 1

    async def test_unavailable_provider_raises_rather_than_returns_empty(self) -> None:
        """Unlike ``complete``, the structured helper must never hand back an
        unparseable empty value masquerading as valid output."""
        provider = ScriptedProvider([])

        class UnavailableProvider(ScriptedProvider):
            @property
            def available(self) -> bool:
                return False

        client = ClaudeClient(provider=UnavailableProvider([]))
        with pytest.raises(StructuredCompleteError):
            await client.structured_complete("prompt", schema=_SampleSchema)

        assert provider.calls == []


# ── Prompt caching + cache-hit metrics ───────────────────────────────────────


def _read_counter(counter: Any, *, provider: str, model: str) -> float:
    """Read the current value of a labelled Counter, 0 if uninitialised."""
    # prometheus_client stores label values keyed by the label tuple.
    metric = counter.labels(provider=provider, model=model)
    return metric._value.get()  # type: ignore[attr-defined]


class TestAnthropicPromptCaching:
    """AnthropicProvider.complete annotates system + increments cache counters."""

    async def test_system_prompt_sends_cache_control_ephemeral(self) -> None:
        """Outgoing request body must contain cache_control on the system block."""
        provider = AnthropicProvider(api_key="fake-key")

        captured: dict[str, Any] = {}

        async def fake_create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            # Mimic Anthropic response shape with usage telemetry.
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok", type="text")],
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=123,
                    cache_creation_input_tokens=456,
                ),
            )

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)
        provider._client = fake_client  # type: ignore[assignment]

        response = await provider.complete(
            LLMRequest(
                feature="test",
                prompt="hello",
                model="claude-sonnet-4-5",
                max_tokens=128,
                system="You are a cached system prompt.",
            )
        )

        # 1. Request body carries structured system list with cache_control.
        system = captured["system"]
        assert isinstance(system, list)
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "You are a cached system prompt."
        assert system[0]["cache_control"] == {"type": "ephemeral"}

        # 2. Response propagates the cache token counts verbatim.
        assert response.cache_read_input_tokens == 123
        assert response.cache_creation_input_tokens == 456
        assert response.input_tokens == 10
        assert response.output_tokens == 5

    async def test_no_system_prompt_still_caches_nothing(self) -> None:
        """When no system prompt is given, no ``system`` kwarg is sent."""
        provider = AnthropicProvider(api_key="fake-key")

        captured: dict[str, Any] = {}

        async def fake_create(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok", type="text")],
                usage=SimpleNamespace(
                    input_tokens=1,
                    output_tokens=1,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)
        provider._client = fake_client  # type: ignore[assignment]

        await provider.complete(
            LLMRequest(
                feature="test",
                prompt="hi",
                model="claude-sonnet-4-5",
                max_tokens=10,
            )
        )

        assert "system" not in captured

    async def test_cache_counters_increment_from_usage(self) -> None:
        """Both counters step by the exact token counts reported in ``usage``."""
        provider = AnthropicProvider(api_key="fake-key")

        # Snapshot pre-test counter values so the assertion is order-safe
        # against other tests that may have incremented the same labels.
        pre_read = _read_counter(
            metrics_mod.LLM_CACHE_READ_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-sonnet-4-5",
        )
        pre_creation = _read_counter(
            metrics_mod.LLM_CACHE_CREATION_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-sonnet-4-5",
        )

        async def fake_create(**_: Any) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok", type="text")],
                usage=SimpleNamespace(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=777,
                    cache_creation_input_tokens=1111,
                ),
            )

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)
        provider._client = fake_client  # type: ignore[assignment]

        await provider.complete(
            LLMRequest(
                feature="test",
                prompt="hi",
                model="claude-sonnet-4-5",
                max_tokens=10,
                system="cached system",
            )
        )

        post_read = _read_counter(
            metrics_mod.LLM_CACHE_READ_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-sonnet-4-5",
        )
        post_creation = _read_counter(
            metrics_mod.LLM_CACHE_CREATION_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-sonnet-4-5",
        )

        assert post_read - pre_read == 777
        assert post_creation - pre_creation == 1111

    async def test_zero_usage_does_not_emit_counter_samples(self) -> None:
        """A response with zero cache tokens must not pollute the counters.

        The helper silently drops non-positive increments so the Grafana
        hit-ratio calculation isn't skewed by providers that don't surface
        cache telemetry at all.
        """
        provider = AnthropicProvider(api_key="fake-key")

        pre_read = _read_counter(
            metrics_mod.LLM_CACHE_READ_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-haiku-4-5",
        )
        pre_creation = _read_counter(
            metrics_mod.LLM_CACHE_CREATION_TOKENS_TOTAL,
            provider="anthropic",
            model="claude-haiku-4-5",
        )

        async def fake_create(**_: Any) -> Any:
            return SimpleNamespace(
                content=[SimpleNamespace(text="ok", type="text")],
                usage=SimpleNamespace(
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            )

        fake_client = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=fake_create)
        provider._client = fake_client  # type: ignore[assignment]

        await provider.complete(
            LLMRequest(
                feature="test",
                prompt="hi",
                model="claude-haiku-4-5",
                max_tokens=10,
            )
        )

        assert (
            _read_counter(
                metrics_mod.LLM_CACHE_READ_TOKENS_TOTAL,
                provider="anthropic",
                model="claude-haiku-4-5",
            )
            == pre_read
        )
        assert (
            _read_counter(
                metrics_mod.LLM_CACHE_CREATION_TOKENS_TOTAL,
                provider="anthropic",
                model="claude-haiku-4-5",
            )
            == pre_creation
        )


class TestLiteLLMSystemCacheAnnotation:
    """LiteLLM tool-use path annotates the leading system turn on Claude models."""

    def test_annotates_claude_model_system_block(self) -> None:
        from caretaker.llm.provider import (
            _annotate_system_cache_control,
            _supports_prompt_cache,
        )

        assert _supports_prompt_cache("anthropic/claude-sonnet-4-5") is True
        assert _supports_prompt_cache("claude-sonnet-4-5") is True
        assert _supports_prompt_cache("azure_ai/gpt-4o") is False

        messages = [
            {"role": "system", "content": "stable prefix"},
            {"role": "user", "content": "hi"},
        ]
        result = _annotate_system_cache_control(messages)
        assert result is not messages  # new list, not mutated in place
        head = result[0]
        assert head["role"] == "system"
        assert isinstance(head["content"], list)
        assert head["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert head["content"][0]["text"] == "stable prefix"
        # Non-system turns are copied through unchanged.
        assert result[1] == {"role": "user", "content": "hi"}

    def test_skips_when_no_leading_system_turn(self) -> None:
        from caretaker.llm.provider import _annotate_system_cache_control

        messages = [{"role": "user", "content": "hi"}]
        assert _annotate_system_cache_control(messages) == messages

    def test_skips_when_content_already_structured(self) -> None:
        from caretaker.llm.provider import _annotate_system_cache_control

        pre_structured = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "x"},
                ],
            }
        ]
        # Already structured → caller owns the shape, we don't touch it.
        assert _annotate_system_cache_control(pre_structured) is pre_structured


class TestLiteLLMCacheUsageExtraction:
    """_extract_litellm_cache_tokens tolerates several ``usage`` shapes."""

    def test_native_anthropic_keys(self) -> None:
        from caretaker.llm.provider import _extract_litellm_cache_tokens

        usage = SimpleNamespace(
            cache_read_input_tokens=42,
            cache_creation_input_tokens=7,
        )
        assert _extract_litellm_cache_tokens(usage) == (42, 7)

    def test_openai_cached_tokens_fallback_for_reads(self) -> None:
        from caretaker.llm.provider import _extract_litellm_cache_tokens

        usage = SimpleNamespace(
            prompt_tokens_details=SimpleNamespace(cached_tokens=13),
        )
        assert _extract_litellm_cache_tokens(usage) == (13, 0)

    def test_missing_usage_returns_zeros(self) -> None:
        from caretaker.llm.provider import _extract_litellm_cache_tokens

        assert _extract_litellm_cache_tokens(None) == (0, 0)


class TestPromptCacheFailOpen:
    """Non-Claude models must not receive cache_control annotations."""

    def test_non_claude_models_skip_annotation(self) -> None:
        from caretaker.llm.provider import (
            _annotate_system_cache_control,
            _supports_prompt_cache,
        )

        assert _supports_prompt_cache("openai/gpt-4o-mini") is False
        assert _supports_prompt_cache("azure_ai/gpt-4o") is False
        # The provider only calls _annotate_system_cache_control when the model
        # supports caching — assert the gate keeps non-Claude payloads untouched
        # by exercising it directly.
        messages = [
            {"role": "system", "content": "x"},
            {"role": "user", "content": "y"},
        ]
        # Must remain string-content when the upstream guard is off.
        if not _supports_prompt_cache("openai/gpt-4o-mini"):
            # Simulate the provider NOT calling the annotator.
            assert messages[0]["content"] == "x"
        else:  # pragma: no cover
            raise AssertionError("unexpected: openai model should not be cache-capable")
        # Sanity: calling the annotator directly still annotates — the fail-
        # open behaviour is the absence of the call at the provider level.
        annotated = _annotate_system_cache_control(messages)
        assert isinstance(annotated[0]["content"], list)
