"""End-to-end smoke: orchestrator-built engine running a readiness decision."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.consensus import active as consensus_active
from caretaker.orchestrator import _build_consensus_engine


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def structured_complete(
        self,
        prompt: str,
        *,
        schema: type[Any],
        feature: str,
        system: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        max_tokens: int = 2000,
    ) -> Any:
        self.calls.append(model or "<default>")
        queue = self.responses.get(model or "<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _reset_active() -> None:
    consensus_active.reset_for_tests()


@pytest.mark.asyncio
async def test_e2e_orchestrator_engine_runs_two_provider_readiness_decision() -> None:
    """End-to-end: build engine via orchestrator helper, swap claude, run readiness."""
    from caretaker.pr_agent.readiness_llm import Readiness

    # Real config types — only the ClaudeClient gets faked.
    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={
                    "reasoning_anthropic": "fake-anthropic",
                    "reasoning_alt": "fake-alt",
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

    engine = _build_consensus_engine(config)
    assert engine is not None
    assert engine.has_site("readiness")

    # Inject a fake claude — the helper built a real ClaudeClient that would
    # try to hit a real provider; swap it for the test.
    fake_claude = _FakeClaude(
        responses={
            "fake-anthropic": [
                Readiness(
                    verdict="ready",  # type: ignore[arg-type]
                    summary="ok",
                    confidence=0.9,
                    blockers=[],
                ),
            ],
            "fake-alt": [
                Readiness(
                    verdict="ready",  # type: ignore[arg-type]
                    summary="ok",
                    confidence=0.85,
                    blockers=[],
                ),
            ],
        },
    )
    engine._claude = fake_claude  # type: ignore[attr-defined]
    consensus_active.configure(engine)

    result = await engine.decide(
        site_name="readiness",
        schema=Readiness,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )

    # Both providers consulted in parallel via AlwaysTwoModels.
    assert sorted(fake_claude.calls) == ["fake-alt", "fake-anthropic"]
    # Both returned "ready" → primary's verdict ships, no escalation.
    assert result.verdict.verdict == "ready"
    assert result.trace.escalated is False
    assert result.trace.final_model == "fake-anthropic"
