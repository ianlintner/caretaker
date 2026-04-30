"""Tests for consensus strategies.

Uses a fake ClaudeClient that returns canned verdicts to exercise the
strategy logic without hitting real LLMs. Each fake call records
which model was requested so the test can assert call shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field

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
    """Fake ClaudeClient used by strategy tests.

    ``responses`` maps concrete model string → list of (verdict | exception)
    consumed in call order. ``calls`` records the model passed on each call.
    """

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
        assert model is not None, "strategy must pass an explicit model"
        self.calls.append(model)
        queue = self.responses.get(model, [])
        if not queue:
            raise StructuredCompleteError(
                raw_text="", validation_error=RuntimeError(f"no canned response for {model}")
            )
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ctx(
    claude: _FakeClaude, *, primary: str, escalation: list[str], threshold: float = 0.7
) -> StrategyContext:
    return StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary=primary,
        escalation=escalation,
        confidence_threshold=threshold,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )


# ── TieredConfidence ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tiered_ships_primary_when_above_threshold() -> None:
    claude = _FakeClaude(
        responses={"fake-fast": [_Verdict(label="ready", confidence=0.9)]},
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is False
    assert result.trace.final_model == "fake-fast"
    assert claude.calls == ["fake-fast"]


@pytest.mark.asyncio
async def test_tiered_escalates_when_below_threshold() -> None:
    claude = _FakeClaude(
        responses={
            "fake-fast": [_Verdict(label="not_ready", confidence=0.4)],
            "fake-strong": [_Verdict(label="ready", confidence=0.95)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is True
    assert result.trace.final_model == "fake-strong"
    assert claude.calls == ["fake-fast", "fake-strong"]


@pytest.mark.asyncio
async def test_tiered_picks_highest_confidence_in_escalation_tier() -> None:
    claude = _FakeClaude(
        responses={
            "fake-fast": [_Verdict(label="not_ready", confidence=0.3)],
            "fake-strong": [_Verdict(label="ready", confidence=0.65)],
            "literal-strongest": [_Verdict(label="needs_human", confidence=0.92)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic", "literal-strongest"])
    result = await strategy.run(ctx)
    # Highest confidence among escalation tier wins.
    assert result.verdict.label == "needs_human"
    assert result.trace.final_model == "literal-strongest"


@pytest.mark.asyncio
async def test_tiered_raises_when_all_models_error() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    with pytest.raises(ConsensusUnavailable) as excinfo:
        await strategy.run(ctx)
    assert excinfo.value.strategy == "tiered_confidence"
    assert len(excinfo.value.attempts) == 2


@pytest.mark.asyncio
async def test_tiered_recovers_when_only_primary_errors() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-fast": [err],
            "fake-strong": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is True
    assert claude.calls == ["fake-fast", "fake-strong"]


@pytest.mark.asyncio
async def test_tiered_primary_wins_ties() -> None:
    """When primary and escalation tie on confidence, primary wins (cheaper default)."""
    claude = _FakeClaude(
        responses={
            "fake-fast": [_Verdict(label="ready", confidence=0.85)],
            "fake-strong": [_Verdict(label="not_ready", confidence=0.85)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"], threshold=0.9)
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.final_model == "fake-fast"
    # escalation still ran (primary was below threshold), so escalated=True
    assert result.trace.escalated is True


# ── AlwaysTwoModels ───────────────────────────────────────────────────────


def _atm_ctx(
    claude: _FakeClaude, *, primary: str, escalation: list[str], agreement: list[str]
) -> StrategyContext:
    return StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary=primary,
        escalation=escalation,
        confidence_threshold=0.7,
        agreement_fields=agreement,
        pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
        claude=claude,
    )


@pytest.mark.asyncio
async def test_atm_ships_primary_when_models_agree() -> None:
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is False
    assert result.trace.final_model == "fake-anthropic"
    # Both must have been called (in parallel).
    assert sorted(claude.calls) == ["fake-alt", "fake-anthropic"]


@pytest.mark.asyncio
async def test_atm_escalates_to_tiebreaker_on_disagreement() -> None:
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
            "literal-tiebreaker": [_Verdict(label="needs_human", confidence=0.7)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    assert result.verdict.label == "needs_human"
    assert result.trace.escalated is True
    assert result.trace.final_model == "literal-tiebreaker"


@pytest.mark.asyncio
async def test_atm_promotes_tiebreaker_when_one_model_errors() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [err],
            "literal-tiebreaker": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    # Two votes ('fake-anthropic' + 'literal-tiebreaker') agree on 'ready'.
    assert result.verdict.label == "ready"
    # final_model is always the model that produced the shipped verdict.
    # Here the tiebreaker agreed with the surviving primary, so the
    # tiebreaker's verdict shipped → final_model is the tiebreaker.
    assert result.trace.final_model == "literal-tiebreaker"


@pytest.mark.asyncio
async def test_atm_raises_when_both_initial_models_error() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [err],
            "fake-alt": [err],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=["label"],
    )
    with pytest.raises(ConsensusUnavailable):
        await strategy.run(ctx)


@pytest.mark.asyncio
async def test_atm_compares_full_verdict_when_agreement_fields_empty() -> None:
    """With agreement_fields=[], the strategy compares full verdicts via __eq__."""
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.9)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=[],
    )
    result = await strategy.run(ctx)
    assert result.trace.escalated is False


@pytest.mark.asyncio
async def test_atm_raises_when_disagreement_and_no_tiebreaker_tier() -> None:
    """When primary and secondary disagree and there's no tiebreaker tier, raise.

    The AlwaysTwoModels contract is "two models must agree (or a tiebreaker
    breaks the tie)." With only escalation[0] configured and the two voters
    disagreeing, we lack both. Raise ConsensusUnavailable so the caller
    falls back to the safe default (e.g. block-merge for readiness).
    """
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],  # no tiebreaker tier
        agreement=["label"],
    )
    with pytest.raises(ConsensusUnavailable):
        await strategy.run(ctx)


@pytest.mark.asyncio
async def test_atm_raises_when_disagreement_and_tiebreaker_tier_exhausted() -> None:
    """Same as above but with a tiebreaker tier where every entry errors."""
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
            "literal-tiebreaker": [err],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        agreement=["label"],
    )
    with pytest.raises(ConsensusUnavailable):
        await strategy.run(ctx)
