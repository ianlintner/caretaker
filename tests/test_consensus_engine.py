"""Tests for ConsensusEngine.decide orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable


class _Verdict(BaseModel):
    label: str
    confidence: float


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
        assert model is not None
        self.calls.append(model)
        queue = self.responses.get(model, [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _engine(claude: _FakeClaude, *, sites: dict[str, SiteConfig]) -> ConsensusEngine:
    return ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool(
                {
                    "fast": "fake-fast",
                    "reasoning_anthropic": "fake-strong",
                    "reasoning_alt": "fake-alt",
                }
            ),
            sites=sites,
        ),
        claude=claude,
    )


@pytest.mark.asyncio
async def test_engine_routes_to_tiered_strategy() -> None:
    claude = _FakeClaude(responses={"fake-fast": [_Verdict(label="ready", confidence=0.95)]})
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="tiered_confidence",
                primary="fast",
                escalation=["reasoning_anthropic"],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    result = await engine.decide(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )
    assert result.verdict.label == "ready"
    assert result.trace.strategy == "tiered_confidence"


@pytest.mark.asyncio
async def test_engine_routes_to_always_two_models() -> None:
    claude = _FakeClaude(
        responses={
            "fake-strong": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="always_two_models",
                primary="reasoning_anthropic",
                escalation=["reasoning_alt"],
                confidence_threshold=0.7,
                agreement_fields=["label"],
            ),
        },
    )
    result = await engine.decide(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )
    assert result.verdict.label == "ready"
    assert result.trace.strategy == "always_two_models"


@pytest.mark.asyncio
async def test_engine_raises_for_unknown_site() -> None:
    engine = _engine(_FakeClaude(), sites={})
    with pytest.raises(KeyError, match=r"site 'unknown_site'"):
        await engine.decide(
            site_name="unknown_site",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )


@pytest.mark.asyncio
async def test_engine_propagates_consensus_unavailable() -> None:
    from caretaker.llm.claude import StructuredCompleteError

    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="tiered_confidence",
                primary="fast",
                escalation=["reasoning_anthropic"],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    with pytest.raises(ConsensusUnavailable):
        await engine.decide(
            site_name="readiness",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )


@pytest.mark.asyncio
async def test_engine_raises_for_unknown_strategy() -> None:
    engine = _engine(
        _FakeClaude(),
        sites={
            "readiness": SiteConfig(
                strategy="unknown_strategy",  # type: ignore[arg-type]
                primary="fast",
                escalation=[],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    with pytest.raises(ValueError, match=r"unknown consensus strategy 'unknown_strategy'"):
        await engine.decide(
            site_name="readiness",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )


@pytest.mark.asyncio
async def test_engine_has_site_returns_true_for_configured_site() -> None:
    engine = _engine(
        _FakeClaude(),
        sites={
            "readiness": SiteConfig(
                strategy="tiered_confidence",
                primary="fast",
                escalation=["reasoning_anthropic"],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    assert engine.has_site("readiness") is True
    assert engine.has_site("size_classifier") is False
