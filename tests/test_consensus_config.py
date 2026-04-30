"""Tests for new consensus-related config models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caretaker.config import ConsensusDomainConfig, LLMConfig, ModelPoolConfig


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


def test_consensus_domain_defaults() -> None:
    cfg = ConsensusDomainConfig()
    assert cfg.strategy == "tiered_confidence"
    assert cfg.primary == "fast"
    assert cfg.escalation == ["reasoning_anthropic"]
    assert cfg.confidence_threshold == 0.7
    assert cfg.agreement_fields == []


def test_consensus_domain_accepts_always_two_with_distinct_escalation() -> None:
    cfg = ConsensusDomainConfig(
        strategy="always_two_models",
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement_fields=["verdict"],
    )
    assert cfg.strategy == "always_two_models"


def test_consensus_domain_rejects_always_two_without_escalation() -> None:
    with pytest.raises(ValidationError, match="escalation"):
        ConsensusDomainConfig(
            strategy="always_two_models",
            primary="reasoning_anthropic",
            escalation=[],
        )


def test_consensus_domain_rejects_always_two_with_same_primary_in_escalation() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        ConsensusDomainConfig(
            strategy="always_two_models",
            primary="reasoning_anthropic",
            escalation=["reasoning_anthropic"],
        )


def test_confidence_threshold_bounded() -> None:
    with pytest.raises(ValidationError):
        ConsensusDomainConfig(confidence_threshold=1.5)
    with pytest.raises(ValidationError):
        ConsensusDomainConfig(confidence_threshold=-0.1)
