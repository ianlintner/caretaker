"""Tests that consensus strategies increment Prometheus counters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field

from caretaker.consensus.metrics import (
    CONSENSUS_DECISIONS_TOTAL,
    CONSENSUS_DISAGREEMENT_TOTAL,
    CONSENSUS_UNAVAILABLE_TOTAL,
)
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable
from caretaker.consensus.strategies import (
    AlwaysTwoModels,
    StrategyContext,
    TieredConfidence,
)
from caretaker.llm.claude import StructuredCompleteError


class _Verdict(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)

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
        queue = self.responses.get(model or "<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_tiered_increments_primary_shipped_counter() -> None:
    before = CONSENSUS_DECISIONS_TOTAL.labels(
        site="readiness", strategy="tiered_confidence", outcome="primary_shipped"
    )._value.get()
    claude = _FakeClaude(responses={"fake-fast": [_Verdict(label="ready", confidence=0.95)]})
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="fast",
        escalation=["reasoning_anthropic"],
        confidence_threshold=0.7,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )
    await TieredConfidence().run(ctx)
    after = CONSENSUS_DECISIONS_TOTAL.labels(
        site="readiness", strategy="tiered_confidence", outcome="primary_shipped"
    )._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_atm_increments_disagreement_counter() -> None:
    before = CONSENSUS_DISAGREEMENT_TOTAL.labels(site="readiness")._value.get()
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
            "literal-tiebreaker": [_Verdict(label="ready", confidence=0.7)],
        },
    )
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        confidence_threshold=0.7,
        agreement_fields=["label"],
        pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
        claude=claude,
    )
    await AlwaysTwoModels().run(ctx)
    after = CONSENSUS_DISAGREEMENT_TOTAL.labels(site="readiness")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_unavailable_counter_increments_on_total_failure() -> None:
    before = CONSENSUS_UNAVAILABLE_TOTAL.labels(site="readiness")._value.get()
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="fast",
        escalation=["reasoning_anthropic"],
        confidence_threshold=0.7,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )
    with pytest.raises(ConsensusUnavailable):
        await TieredConfidence().run(ctx)
    after = CONSENSUS_UNAVAILABLE_TOTAL.labels(site="readiness")._value.get()
    assert after == before + 1
