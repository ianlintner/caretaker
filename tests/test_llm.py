"""Tests for the LLM client, provider abstraction, and routing."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from caretaker.config import FeatureModelConfig, LLMConfig
from caretaker.llm.claude import ClaudeClient
from caretaker.llm.provider import (
    AnthropicProvider,
    LiteLLMProvider,
    LLMRequest,
    LLMResponse,
    NullProvider,
    build_provider,
)
from caretaker.llm.router import LLMRouter

if TYPE_CHECKING:
    import pytest


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
                "ci_log_analysis": FeatureModelConfig(
                    model="openai/gpt-4o-mini", max_tokens=500
                ),
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
        with patch.dict(
            os.environ, {"AZURE_AI_API_KEY": "foundry-key"}, clear=True
        ):
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
