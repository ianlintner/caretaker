"""End-to-end test that ConsensusEngine traces are persisted onto ShadowDecisionRecord."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from caretaker.consensus import active as consensus_active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt
from caretaker.consensus.trace_context import current_trace_var
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import (
    clear_records_for_tests,
    recent_records,
    shadow_decision,
)


class _Verdict(BaseModel):
    label: str
    confidence: float


class _FakeClaude:
    def __init__(self, response: Any) -> None:
        self.response = response

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
        return self.response


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset cross-test state."""
    consensus_active.reset_for_tests()
    shadow_config.reset_for_tests()
    clear_records_for_tests()
    # Clear the contextvar; ContextVar.reset() requires a token, so use set(default).
    current_trace_var.set(None)


# ── Engine sets the contextvar ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_decide_sets_trace_contextvar() -> None:
    """When engine.decide succeeds, current_trace_var holds the trace afterward."""
    claude = _FakeClaude(response=_Verdict(label="ready", confidence=0.95))
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool({"fast": "fake-fast"}),
            sites={
                "readiness": SiteConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=[],
                    confidence_threshold=0.7,
                    agreement_fields=[],
                ),
            },
        ),
        claude=claude,
    )
    assert current_trace_var.get() is None

    result = await engine.decide(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )

    captured = current_trace_var.get()
    assert captured is not None
    assert captured is result.trace
    assert captured.strategy == "tiered_confidence"
    assert captured.final_model == "fake-fast"


# ── @shadow_decision reads the contextvar in shadow mode ──────────────────


def _agentic_config_with(*, name: str, mode: str) -> Any:
    """Build a minimal AgenticConfig that sets the named site to mode."""
    from caretaker.config import AgenticConfig, AgenticDomainConfig

    cfg = AgenticConfig()
    domain: AgenticDomainConfig = getattr(cfg, name)
    object.__setattr__(domain, "mode", mode)
    return cfg


@pytest.mark.asyncio
async def test_shadow_mode_persists_trace_when_set() -> None:
    """In shadow mode, the trace from the contextvar is serialised onto the record."""
    shadow_config.configure(_agentic_config_with(name="readiness", mode="shadow"))

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any) -> _Verdict:
        return _Verdict(label="ready", confidence=0.9)

    async def legacy_fn() -> _Verdict:
        return _Verdict(label="not_ready", confidence=0.5)

    async def candidate_fn() -> _Verdict:
        # Simulate that engine.decide ran and set the contextvar.
        trace = ConsensusTrace(
            strategy="tiered_confidence",
            attempts=[
                ModelAttempt(
                    model="claude-fast",
                    tag="fast",
                    latency_ms=120,
                    confidence=0.9,
                    verdict_summary="ready",
                ),
            ],
            escalated=False,
            final_model="claude-fast",
        )
        current_trace_var.set(trace)
        return _Verdict(label="ready", confidence=0.9)

    await decide(legacy=legacy_fn, candidate=candidate_fn)

    records = recent_records(name="readiness")
    assert len(records) == 1
    assert records[0].consensus_trace_json is not None
    payload = json.loads(records[0].consensus_trace_json)
    assert payload["strategy"] == "tiered_confidence"
    assert payload["final_model"] == "claude-fast"
    assert len(payload["attempts"]) == 1


@pytest.mark.asyncio
async def test_shadow_mode_no_trace_when_legacy_only() -> None:
    """If candidate didn't run engine.decide (no contextvar set), record has trace=None."""
    shadow_config.configure(_agentic_config_with(name="readiness", mode="shadow"))

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any) -> _Verdict:
        return _Verdict(label="ready", confidence=0.9)

    async def legacy_fn() -> _Verdict:
        return _Verdict(label="ready", confidence=0.5)

    async def candidate_fn() -> _Verdict:
        # Candidate runs but does NOT use the engine — trace contextvar stays None.
        return _Verdict(label="ready", confidence=0.9)

    await decide(legacy=legacy_fn, candidate=candidate_fn)

    records = recent_records(name="readiness")
    assert len(records) == 1
    assert records[0].consensus_trace_json is None


# ── Enforce mode now writes records (and includes the trace) ──────────────


@pytest.mark.asyncio
async def test_enforce_mode_writes_record_with_trace() -> None:
    """In enforce mode, a record is written with consensus_trace_json populated.

    Pre-fix, enforce mode wrote no record at all. This test locks in the new
    behaviour so the trace is captured for the very mode readiness defaults to.
    """
    shadow_config.configure(_agentic_config_with(name="readiness", mode="enforce"))

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any) -> _Verdict:
        return _Verdict(label="ready", confidence=0.9)

    async def legacy_fn() -> _Verdict:
        return _Verdict(label="not_ready", confidence=0.5)

    async def candidate_fn() -> _Verdict:
        trace = ConsensusTrace(
            strategy="always_two_models",
            attempts=[
                ModelAttempt(
                    model="claude-anthropic",
                    tag="reasoning_anthropic",
                    latency_ms=412,
                    confidence=0.92,
                    verdict_summary="ready",
                ),
                ModelAttempt(
                    model="openai-gpt",
                    tag="reasoning_alt",
                    latency_ms=380,
                    confidence=0.88,
                    verdict_summary="ready",
                ),
            ],
            escalated=False,
            final_model="claude-anthropic",
        )
        current_trace_var.set(trace)
        return _Verdict(label="ready", confidence=0.92)

    result = await decide(legacy=legacy_fn, candidate=candidate_fn)
    assert result.label == "ready"  # candidate verdict shipped (enforce mode)

    records = recent_records(name="readiness")
    assert len(records) == 1
    assert records[0].outcome == "enforced_candidate"
    assert records[0].mode == "enforce"
    assert records[0].consensus_trace_json is not None
    payload = json.loads(records[0].consensus_trace_json)
    assert payload["strategy"] == "always_two_models"
    assert len(payload["attempts"]) == 2


@pytest.mark.asyncio
async def test_enforce_mode_candidate_error_writes_fallthrough_record() -> None:
    """Candidate raises in enforce mode → record outcome=candidate_error written."""
    shadow_config.configure(_agentic_config_with(name="readiness", mode="enforce"))

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any) -> _Verdict:
        return _Verdict(label="ready", confidence=0.9)

    async def legacy_fn() -> _Verdict:
        return _Verdict(label="not_ready", confidence=0.5)

    async def candidate_fn() -> _Verdict:
        raise RuntimeError("simulated engine failure")

    # Candidate raises; enforce should fall through to legacy.
    result = await decide(legacy=legacy_fn, candidate=candidate_fn)
    assert result.label == "not_ready"  # legacy ran

    records = recent_records(name="readiness")
    assert len(records) == 1
    assert records[0].outcome == "candidate_error"
    assert records[0].mode == "enforce"
    # No trace was set → trace remains None.
    assert records[0].consensus_trace_json is None


@pytest.mark.asyncio
async def test_enforce_mode_candidate_returns_none_writes_fallthrough_record() -> None:
    """Candidate returns None in enforce mode → record outcome=candidate_error."""
    shadow_config.configure(_agentic_config_with(name="readiness", mode="enforce"))

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any) -> _Verdict | None:
        return None

    async def legacy_fn() -> _Verdict:
        return _Verdict(label="not_ready", confidence=0.5)

    async def candidate_fn() -> _Verdict | None:
        return None

    result = await decide(legacy=legacy_fn, candidate=candidate_fn)
    assert result is not None and result.label == "not_ready"

    records = recent_records(name="readiness")
    assert len(records) == 1
    assert records[0].outcome == "candidate_error"
