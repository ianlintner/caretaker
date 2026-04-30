"""Tests for new consensus-related config models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caretaker.config import LLMConfig, ModelPoolConfig


def test_model_pool_config_defaults_empty() -> None:
    cfg = ModelPoolConfig()
    assert cfg.pool == {}


def test_model_pool_config_accepts_tags() -> None:
    cfg = ModelPoolConfig(pool={"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    assert cfg.pool["fast"] == "haiku-4-5"


def test_llm_config_has_model_pool_default() -> None:
    cfg = LLMConfig()
    assert cfg.model_pool.pool == {}


def test_llm_config_accepts_pool() -> None:
    cfg = LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "haiku-4-5"}))
    assert cfg.model_pool.pool["fast"] == "haiku-4-5"


def test_model_pool_rejects_empty_value() -> None:
    with pytest.raises(ValidationError):
        ModelPoolConfig(pool={"fast": ""})
