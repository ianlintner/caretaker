"""Test that Orchestrator constructs and installs a ConsensusEngine when sites opt in."""

from __future__ import annotations

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.consensus import active as consensus_active


def test_orchestrator_init_installs_consensus_when_any_site_opts_in() -> None:
    """When at least one AgenticDomainConfig has consensus set, install the engine."""
    consensus_active.reset_for_tests()

    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={
                    "fast": "claude-haiku-4-5",
                    "reasoning_anthropic": "claude-sonnet-4-6",
                    "reasoning_alt": "openai/gpt-4o",
                },
            ),
        ),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="always_two_models",
                    primary="reasoning_anthropic",
                    escalation=["reasoning_alt"],
                    agreement_fields=["verdict"],
                ),
            ),
        ),
    )

    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is not None
    assert engine.has_site("readiness") is True


def test_orchestrator_init_skips_consensus_when_no_site_opts_in() -> None:
    consensus_active.reset_for_tests()

    config = MaintainerConfig()  # all defaults — no consensus anywhere

    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is None


def test_orchestrator_init_size_classifier_consensus() -> None:
    consensus_active.reset_for_tests()

    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={"fast": "claude-haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"},
            ),
        ),
        agentic=AgenticConfig(
            size_classifier=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                ),
            ),
        ),
    )

    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is not None
    assert engine.has_site("size_classifier") is True
