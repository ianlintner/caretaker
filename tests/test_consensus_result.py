"""Tests for ConsensusResult and ConsensusUnavailable."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt


class _Verdict(BaseModel):
    label: str
    confidence: float


def test_consensus_result_carries_verdict_and_trace() -> None:
    verdict = _Verdict(label="ready", confidence=0.91)
    trace = ConsensusTrace(
        strategy="tiered_confidence",
        attempts=[
            ModelAttempt(
                model="claude-sonnet-4-6",
                tag="reasoning_anthropic",
                latency_ms=412,
                confidence=0.91,
                verdict_summary="ready",
            ),
        ],
        final_model="claude-sonnet-4-6",
    )
    result = ConsensusResult(verdict=verdict, trace=trace)
    assert result.verdict is verdict
    assert result.trace is trace


def test_consensus_unavailable_carries_attempts() -> None:
    attempts = [
        ModelAttempt(
            model="haiku-4-5",
            tag="fast",
            latency_ms=2000,
            error="timeout",
        ),
    ]
    exc = ConsensusUnavailable(
        strategy="tiered_confidence",
        attempts=attempts,
        reason="all tiers exhausted",
    )
    assert exc.strategy == "tiered_confidence"
    assert exc.attempts == attempts
    assert exc.reason == "all tiers exhausted"
    assert "tiered_confidence" in str(exc)


def test_consensus_unavailable_is_exception() -> None:
    with pytest.raises(ConsensusUnavailable):
        raise ConsensusUnavailable(
            strategy="tiered_confidence",
            attempts=[],
            reason="nothing tried",
        )
