"""Tests for ConsensusTrace and ModelAttempt audit records."""

from __future__ import annotations

from caretaker.consensus.trace import ConsensusTrace, ModelAttempt


def test_model_attempt_roundtrip() -> None:
    attempt = ModelAttempt(
        model="claude-sonnet-4-6",
        tag="reasoning_anthropic",
        latency_ms=412,
        confidence=0.82,
        verdict_summary="ready",
        error=None,
    )
    payload = attempt.model_dump_json()
    decoded = ModelAttempt.model_validate_json(payload)
    assert decoded == attempt


def test_consensus_trace_records_strategy_and_attempts() -> None:
    trace = ConsensusTrace(
        strategy="tiered_confidence",
        attempts=[
            ModelAttempt(
                model="haiku-4-5",
                tag="fast",
                latency_ms=120,
                confidence=0.55,
                verdict_summary="not_ready",
            ),
            ModelAttempt(
                model="claude-sonnet-4-6",
                tag="reasoning_anthropic",
                latency_ms=412,
                confidence=0.91,
                verdict_summary="ready",
            ),
        ],
        escalated=True,
        final_model="claude-sonnet-4-6",
    )
    assert trace.escalated is True
    assert len(trace.attempts) == 2
    assert trace.final_model == "claude-sonnet-4-6"

    payload = trace.model_dump_json()
    decoded = ConsensusTrace.model_validate_json(payload)
    assert decoded == trace


def test_model_attempt_with_error_records_failure() -> None:
    attempt = ModelAttempt(
        model="claude-sonnet-4-6",
        tag="reasoning_anthropic",
        latency_ms=4200,
        confidence=None,
        verdict_summary=None,
        error="StructuredCompleteError: timeout",
    )
    assert attempt.error == "StructuredCompleteError: timeout"
    assert attempt.confidence is None
