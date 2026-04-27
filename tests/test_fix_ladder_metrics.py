"""Tests for fix-ladder Prometheus metrics wiring (Wave A3)."""

from __future__ import annotations

from caretaker.observability.metrics import (
    FIX_LADDER_ESCALATION_TOTAL,
    FIX_LADDER_OUTCOME_TOTAL,
    record_fix_ladder_escalation,
    record_fix_ladder_outcome,
)


def _counter_value(counter, **labels):  # type: ignore[no-untyped-def]
    """Read a counter value by label dict — works with the shared REGISTRY."""
    return counter.labels(**labels)._value.get()  # noqa: SLF001 - internal for tests


class TestFixLadderMetrics:
    def test_outcome_counter_increments(self) -> None:
        before = _counter_value(
            FIX_LADDER_OUTCOME_TOTAL, repo="o/r", rung="ruff-format", outcome="progress"
        )
        record_fix_ladder_outcome("o/r", "ruff-format", "progress")
        after = _counter_value(
            FIX_LADDER_OUTCOME_TOTAL, repo="o/r", rung="ruff-format", outcome="progress"
        )
        assert after == before + 1

    def test_escalation_counter_increments(self) -> None:
        before = _counter_value(FIX_LADDER_ESCALATION_TOTAL, repo="o/r", error_sig_hash="abc123")
        record_fix_ladder_escalation("o/r", "abc123")
        after = _counter_value(FIX_LADDER_ESCALATION_TOTAL, repo="o/r", error_sig_hash="abc123")
        assert after == before + 1

    def test_defaults_unknown_when_missing_labels(self) -> None:
        # Empty strings coerce to "unknown" so the label cardinality
        # stays bounded even when the caller has no context.
        record_fix_ladder_outcome("", "ruff-format", "no_progress")
        value = _counter_value(
            FIX_LADDER_OUTCOME_TOTAL,
            repo="unknown",
            rung="ruff-format",
            outcome="no_progress",
        )
        assert value >= 1
