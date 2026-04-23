"""Tests for the model-string-driven LLM env inference in ``doctor``.

Closes #508. These tests verify that ``caretaker doctor`` infers the
required LLM provider env vars from :attr:`LLMConfig.default_model`,
:attr:`LLMConfig.feature_models`, and :attr:`LLMConfig.fallback_models`
instead of relying solely on :attr:`LLMConfig.provider`. The motivating
bug was that every consumer repo using LiteLLM (provider="litellm")
had to author a ``claude_enabled: "false"`` workaround to avoid a
spurious FAIL on ``ANTHROPIC_API_KEY`` even when the configured model
lived on a completely different provider.
"""

from __future__ import annotations

from typing import Any

from caretaker.config import MaintainerConfig
from caretaker.doctor import Severity, check_env_secrets

# ── Helpers ───────────────────────────────────────────────────────────


def _load_config(overrides: dict[str, Any] | None = None) -> MaintainerConfig:
    """Build a :class:`MaintainerConfig` with optional block overrides."""
    data: dict[str, Any] = {"version": "v1"}
    if overrides:
        data.update(overrides)
    return MaintainerConfig.model_validate(data)


def _llm_rows(rows: list[Any]) -> list[Any]:
    """Filter to just the ``category="llm"`` rows for focused asserts."""
    return [r for r in rows if r.category == "llm"]


def _row(rows: list[Any], name: str) -> Any:
    """Fetch the first row with ``name``; raises if absent (clear failure)."""
    return next(r for r in rows if r.name == name)


# ── Tests ─────────────────────────────────────────────────────────────


def test_default_model_azure_ai_requires_azure_ai_key() -> None:
    """``azure_ai/gpt-4.1-mini`` default → FAIL on AZURE_AI_* when env empty."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
            }
        }
    )
    rows = _llm_rows(check_env_secrets(config, {"GITHUB_TOKEN": "x"}))
    names = {r.name for r in rows}
    assert "AZURE_AI_API_KEY" in names
    assert "AZURE_AI_API_BASE" in names
    # And critically: no spurious ANTHROPIC_API_KEY row.
    assert "ANTHROPIC_API_KEY" not in names
    assert _row(rows, "AZURE_AI_API_KEY").severity is Severity.FAIL
    assert _row(rows, "AZURE_AI_API_BASE").severity is Severity.FAIL


def test_default_model_azure_ai_ok_when_both_env_present() -> None:
    """AZURE_AI_* both set → OK rows for both."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
            }
        }
    )
    env = {
        "GITHUB_TOKEN": "x",
        "AZURE_AI_API_KEY": "k",
        "AZURE_AI_API_BASE": "https://foundry.example/openai",
    }
    rows = _llm_rows(check_env_secrets(config, env))
    assert _row(rows, "AZURE_AI_API_KEY").severity is Severity.OK
    assert _row(rows, "AZURE_AI_API_BASE").severity is Severity.OK


def test_feature_model_pulls_in_second_env() -> None:
    """A feature override on a *different* provider adds its own env rows."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
                "feature_models": {
                    "architectural_review": {"model": "azure/gpt-4o"},
                },
            }
        }
    )
    env = {
        "GITHUB_TOKEN": "x",
        "AZURE_AI_API_KEY": "k",
        "AZURE_AI_API_BASE": "https://foundry.example/openai",
    }
    rows = _llm_rows(check_env_secrets(config, env))
    # The AZURE_AI_* rows should be OK — satisfied by env.
    assert _row(rows, "AZURE_AI_API_KEY").severity is Severity.OK
    assert _row(rows, "AZURE_AI_API_BASE").severity is Severity.OK
    # The azure/ (Azure OpenAI) model pulls AZURE_API_* → FAIL.
    assert _row(rows, "AZURE_API_KEY").severity is Severity.FAIL
    assert _row(rows, "AZURE_API_BASE").severity is Severity.FAIL


def test_fallback_model_missing_env_is_warn_not_fail() -> None:
    """Fallback chain is best-effort — missing env downgrades to WARN."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
                "fallback_models": ["gemini/gemini-2.0-flash"],
            }
        }
    )
    env = {
        "GITHUB_TOKEN": "x",
        "AZURE_AI_API_KEY": "k",
        "AZURE_AI_API_BASE": "https://foundry.example/openai",
    }
    rows = _llm_rows(check_env_secrets(config, env))
    gemini = _row(rows, "GEMINI_API_KEY")
    assert gemini.severity is Severity.WARN


def test_vertex_prefix_wins_over_claude_substring() -> None:
    """``vertex_ai/claude-sonnet-4`` routes through Vertex, NOT Anthropic."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "vertex_ai/claude-sonnet-4",
            }
        }
    )
    rows = _llm_rows(check_env_secrets(config, {"GITHUB_TOKEN": "x"}))
    names = {r.name for r in rows}
    assert "GOOGLE_APPLICATION_CREDENTIALS" in names
    assert "VERTEX_PROJECT" in names
    assert "ANTHROPIC_API_KEY" not in names


def test_unknown_prefix_emits_warn_not_fail() -> None:
    """A model whose prefix we don't recognise → informational WARN row only."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "custom/weird-model-name",
            }
        }
    )
    rows = _llm_rows(check_env_secrets(config, {"GITHUB_TOKEN": "x"}))
    # Exactly one LLM row — the UNKNOWN sentinel.
    assert len(rows) == 1
    unknown = rows[0]
    assert unknown.name == "UNKNOWN"
    assert unknown.severity is Severity.WARN
    # No FAIL rows at all — the whole point is "don't hard-fail".
    assert not any(r.severity is Severity.FAIL for r in rows)


def test_legacy_provider_anthropic_still_works() -> None:
    """``provider=anthropic`` short-circuits to a single ANTHROPIC_API_KEY row."""
    config = _load_config(
        {
            "llm": {
                "provider": "anthropic",
                "default_model": "claude-3-5-sonnet-latest",
            }
        }
    )
    rows = check_env_secrets(config, {"GITHUB_TOKEN": "x"})
    # Exactly one LLM row, which is ANTHROPIC_API_KEY at FAIL (env empty).
    # But note: the legacy path still renders via the old "secrets" category,
    # so we look at both categories.
    ant_rows = [r for r in rows if r.name == "ANTHROPIC_API_KEY"]
    assert len(ant_rows) == 1, f"expected a single ANTHROPIC_API_KEY row, got {ant_rows}"
    assert ant_rows[0].severity is Severity.FAIL


def test_dedup_when_two_models_share_env() -> None:
    """Two models on the same provider → one env row, not two."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
                "feature_models": {
                    "architectural_review": {"model": "azure_ai/gpt-4o"},
                },
            }
        }
    )
    rows = _llm_rows(check_env_secrets(config, {"GITHUB_TOKEN": "x"}))
    azure_ai_key_rows = [r for r in rows if r.name == "AZURE_AI_API_KEY"]
    assert len(azure_ai_key_rows) == 1
    azure_ai_base_rows = [r for r in rows if r.name == "AZURE_AI_API_BASE"]
    assert len(azure_ai_base_rows) == 1
