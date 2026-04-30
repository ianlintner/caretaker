"""Test that ShadowDecisionRecord carries an optional consensus_trace_json."""

from __future__ import annotations

from datetime import UTC, datetime

from caretaker.evolution.shadow import ShadowDecisionRecord


def test_shadow_record_has_consensus_trace_field() -> None:
    record = ShadowDecisionRecord(
        id="abc",
        name="readiness",
        run_at=datetime.now(UTC),
        outcome="enforced_candidate",
        mode="enforce",
        legacy_verdict_json="null",
        candidate_verdict_json='{"verdict":"ready"}',
        consensus_trace_json='{"strategy":"tiered_confidence","attempts":[],"escalated":false,"final_model":"x"}',
    )
    assert record.consensus_trace_json is not None


def test_shadow_record_consensus_trace_defaults_none() -> None:
    record = ShadowDecisionRecord(
        id="abc",
        name="readiness",
        run_at=datetime.now(UTC),
        outcome="agree",
        mode="shadow",
        legacy_verdict_json="null",
    )
    assert record.consensus_trace_json is None
