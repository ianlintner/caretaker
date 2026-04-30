"""Tests that doctor.py flags consensus config issues."""

from __future__ import annotations

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.doctor import diagnose_consensus_config


def test_diagnose_passes_when_tags_present_in_pool() -> None:
    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={"fast": "claude-haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"},
            ),
        ),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert issues == []


def test_diagnose_flags_missing_tag_and_keeps_literal_pass_through() -> None:
    config = MaintainerConfig(
        llm=LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "claude-haiku-4-5"})),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic_TYPO"],  # tag-shaped, not in pool
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert any("reasoning_anthropic_TYPO" in issue for issue in issues)


def test_diagnose_does_not_flag_literal_model_strings() -> None:
    """Literals (strings with '/' or starting with a known prefix) pass through."""
    config = MaintainerConfig(
        llm=LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "claude-haiku-4-5"})),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["openai/gpt-4o"],  # literal — pool miss is OK
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert issues == []
