"""Tests for ProviderPool."""

from __future__ import annotations

import pytest

from caretaker.consensus.provider_pool import ProviderPool, ProviderPoolError


def test_resolves_known_tag_to_concrete_model() -> None:
    pool = ProviderPool({"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    assert pool.resolve("fast") == ("haiku-4-5", "fast")
    assert pool.resolve("reasoning_anthropic") == ("claude-sonnet-4-6", "reasoning_anthropic")


def test_literal_model_string_passes_through() -> None:
    """When the caller provides a literal model string, it's returned unchanged.

    Heuristic for "literal vs tag": literal contains a slash, dot, or hyphen
    that's not in any pool key. We treat any value that's not a pool key as
    a literal and pass it through to the LLM router.
    """
    pool = ProviderPool({"fast": "haiku-4-5"})
    assert pool.resolve("openai/gpt-4o") == ("openai/gpt-4o", "openai/gpt-4o")
    assert pool.resolve("claude-sonnet-4-6") == ("claude-sonnet-4-6", "claude-sonnet-4-6")


def test_unknown_empty_value_raises() -> None:
    pool = ProviderPool({"fast": "haiku-4-5"})
    with pytest.raises(ProviderPoolError):
        pool.resolve("")


def test_resolve_distinct_returns_two_distinct_models() -> None:
    pool = ProviderPool({"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    primary_model, _ = pool.resolve("fast")
    second_model, _ = pool.resolve_distinct("reasoning_anthropic", different_from=primary_model)
    assert second_model == "claude-sonnet-4-6"
    assert second_model != primary_model


def test_resolve_distinct_raises_when_same_concrete_model() -> None:
    """resolve_distinct raises when two tags resolve to the same model.

    This is the validation hook for AlwaysTwoModels — if 'reasoning_anthropic'
    and 'reasoning_alt' both happen to point at the same concrete model,
    the strategy must fail-fast at config time, not silently consult the
    same model twice.
    """
    pool = ProviderPool({"r1": "claude-sonnet-4-6", "r2": "claude-sonnet-4-6"})
    with pytest.raises(ProviderPoolError):
        pool.resolve_distinct("r2", different_from="claude-sonnet-4-6")


def test_pool_construction_rejects_empty_value() -> None:
    with pytest.raises(ProviderPoolError):
        ProviderPool({"fast": ""})
