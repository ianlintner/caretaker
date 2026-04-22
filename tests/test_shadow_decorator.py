"""Tests for the @shadow_decision decorator + ShadowDecision graph write.

Covers every branch called out in T-D1 Part E:

* ``off`` — legacy-only.
* ``shadow-agree`` — both paths run, no disagreement recorded.
* ``shadow-disagree`` — both paths run, disagreement recorded.
* ``shadow-candidate-raises`` — candidate swallowed, legacy returned.
* ``enforce-candidate-returns`` — candidate authoritative.
* ``enforce-candidate-raises-falls-through`` — legacy is the safety net.

Also verifies the graph writer path (fake Neo4j store) and the
no-Neo4j log fallback.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import (
    SHADOW_DECISIONS_TOTAL,
    ShadowDecisionRecord,
    clear_records_for_tests,
    recent_records,
    shadow_decision,
    write_shadow_decision,
)
from caretaker.graph import writer as graph_writer

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    """Clear cross-test state (ring buffer, active config, graph writer)."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    graph_writer.reset_for_tests()


def _set_mode(name: str, mode: str) -> None:
    """Install an ``AgenticConfig`` with ``<name>.mode = mode``."""
    kwargs: dict[str, AgenticDomainConfig] = {
        name: AgenticDomainConfig(mode=mode)  # type: ignore[arg-type]
    }
    cfg = AgenticConfig(**kwargs)
    shadow_config.configure(cfg)


# ── Decorator: off ───────────────────────────────────────────────────────


async def test_off_mode_returns_legacy_and_skips_candidate() -> None:
    _set_mode("readiness", "off")

    legacy = AsyncMock(return_value="LEGACY")
    candidate = AsyncMock(return_value="CANDIDATE")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper should short-circuit off-mode")

    result = await decide(legacy=legacy, candidate=candidate)

    assert result == "LEGACY"
    legacy.assert_awaited_once()
    candidate.assert_not_awaited()
    # No record should have been written in off-mode.
    assert recent_records() == []


# ── Decorator: shadow / agree ────────────────────────────────────────────


async def test_shadow_agree_returns_legacy_and_writes_no_disagreement_reason() -> None:
    _set_mode("readiness", "shadow")

    legacy = AsyncMock(return_value={"ready": True})
    candidate = AsyncMock(return_value={"ready": True})

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> dict[str, bool]:
        raise AssertionError("wrapper is the one doing the work")

    result = await decide(
        legacy=legacy,
        candidate=candidate,
        context={"repo_slug": "ian/demo", "pr_number": 7},
    )

    assert result == {"ready": True}
    legacy.assert_awaited_once()
    candidate.assert_awaited_once()

    records = recent_records()
    assert len(records) == 1
    rec = records[0]
    assert rec.outcome == "agree"
    assert rec.name == "readiness"
    assert rec.mode == "shadow"
    assert rec.disagreement_reason is None
    assert rec.repo_slug == "ian/demo"
    assert rec.candidate_verdict_json is not None
    assert "ready" in rec.candidate_verdict_json


# ── Decorator: shadow / disagree ─────────────────────────────────────────


async def test_shadow_disagree_records_reason_and_still_returns_legacy() -> None:
    _set_mode("ci_triage", "shadow")

    @shadow_decision("ci_triage")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives the call")

    async def legacy() -> str:
        return "flaky"

    async def candidate() -> str:
        return "real-failure"

    result = await decide(legacy=legacy, candidate=candidate)

    assert result == "flaky"  # legacy wins in shadow mode
    records = recent_records()
    assert len(records) == 1
    assert records[0].outcome == "disagree"
    assert records[0].disagreement_reason is not None
    assert "flaky" in records[0].disagreement_reason
    assert "real-failure" in records[0].disagreement_reason


async def test_shadow_custom_compare_function() -> None:
    _set_mode("review_classification", "shadow")

    def compare(a: Any, b: Any) -> bool:
        # Ignore the rationale field when comparing verdicts.
        return a["label"] == b["label"]

    @shadow_decision("review_classification", compare=compare)
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> dict[str, str]:
        raise AssertionError("wrapper drives")

    async def legacy() -> dict[str, str]:
        return {"label": "nit", "rationale": "no issues"}

    async def candidate() -> dict[str, str]:
        return {"label": "nit", "rationale": "different wording"}

    await decide(legacy=legacy, candidate=candidate)
    records = recent_records()
    assert len(records) == 1
    assert records[0].outcome == "agree"


# ── Decorator: shadow / candidate raises ─────────────────────────────────


async def test_shadow_candidate_raises_is_swallowed() -> None:
    _set_mode("readiness", "shadow")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    async def legacy() -> str:
        return "OK"

    async def candidate() -> str:
        raise RuntimeError("boom")

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "OK"
    records = recent_records()
    assert len(records) == 1
    assert records[0].outcome == "candidate_error"
    assert records[0].candidate_verdict_json is None
    assert records[0].disagreement_reason is not None
    assert "RuntimeError" in records[0].disagreement_reason


# ── Decorator: enforce / candidate returns ───────────────────────────────


async def test_enforce_candidate_returns_is_authoritative() -> None:
    _set_mode("readiness", "enforce")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    legacy = AsyncMock(return_value="LEGACY")
    candidate = AsyncMock(return_value="CANDIDATE")

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "CANDIDATE"
    candidate.assert_awaited_once()
    legacy.assert_not_awaited()


async def test_enforce_candidate_returns_none_falls_through_to_legacy() -> None:
    _set_mode("readiness", "enforce")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    legacy = AsyncMock(return_value="LEGACY")
    candidate = AsyncMock(return_value=None)

    result = await decide(legacy=legacy, candidate=candidate)
    assert result == "LEGACY"
    legacy.assert_awaited_once()


# ── Decorator: enforce / candidate raises ────────────────────────────────


async def test_enforce_candidate_raises_falls_through_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_mode("readiness", "enforce")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    async def legacy() -> str:
        return "LEGACY"

    async def candidate() -> str:
        raise ValueError("bad model")

    with caplog.at_level(logging.WARNING, logger="caretaker.evolution.shadow"):
        result = await decide(legacy=legacy, candidate=candidate)

    assert result == "LEGACY"
    assert any("falling through" in rec.message for rec in caplog.records)


# ── Decorator validation ─────────────────────────────────────────────────


def test_decorator_rejects_sync_function() -> None:
    with pytest.raises(TypeError, match="async function"):

        @shadow_decision("readiness")
        def sync_decide(*, legacy: Any, candidate: Any) -> str:  # pragma: no cover
            return "nope"


async def test_decorator_rejects_call_without_legacy_candidate() -> None:
    _set_mode("readiness", "shadow")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError("wrapper drives")

    with pytest.raises(TypeError, match="legacy"):
        await decide()  # type: ignore[call-arg]


# ── Graph writer integration ─────────────────────────────────────────────


class _FakeGraphStore:
    """Minimal ``GraphStore`` shim capturing ``merge_node`` calls."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, str, dict[str, Any]]] = []

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        self.nodes.append((label, node_id, properties))

    async def merge_edge(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("shadow writer never emits edges")


async def test_write_shadow_decision_goes_to_graph_when_enabled() -> None:
    store = _FakeGraphStore()
    writer = graph_writer.get_writer()
    writer.configure(store)  # type: ignore[arg-type]
    await writer.start()
    try:
        from datetime import UTC, datetime

        rec = ShadowDecisionRecord(
            id="abc",
            name="readiness",
            repo_slug="ian/demo",
            run_at=datetime.now(UTC),
            outcome="disagree",
            mode="shadow",
            legacy_verdict_json='{"ready": true}',
            candidate_verdict_json='{"ready": false}',
            disagreement_reason="legacy says ready, candidate says not",
            context_json='{"pr": 7}',
        )
        write_shadow_decision(rec)
        await writer.flush(timeout=2.0)
    finally:
        await writer.stop()
        graph_writer.reset_for_tests()

    assert len(store.nodes) == 1
    label, node_id, props = store.nodes[0]
    assert label == "ShadowDecision"
    assert node_id == "abc"
    assert props["name"] == "readiness"
    assert props["outcome"] == "disagree"
    assert props["candidate_verdict_json"] == '{"ready": false}'


async def test_write_shadow_decision_falls_back_to_log_when_graph_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Writer left disabled by the autouse fixture.
    from datetime import UTC, datetime

    rec = ShadowDecisionRecord(
        id="xyz",
        name="ci_triage",
        repo_slug="ian/demo",
        run_at=datetime.now(UTC),
        outcome="disagree",
        mode="shadow",
        legacy_verdict_json='"flaky"',
        candidate_verdict_json='"real-failure"',
        disagreement_reason="classification mismatch",
        context_json="{}",
    )
    with caplog.at_level(logging.INFO, logger="caretaker.evolution.shadow"):
        write_shadow_decision(rec)

    # One structured event line — must be reconstructable.
    matching = [r for r in caplog.records if "shadow_decision" in r.message]
    assert matching, "expected a shadow_decision log line"
    msg = matching[0].message
    assert "id=xyz" in msg
    assert "name=ci_triage" in msg
    assert "outcome=disagree" in msg
    assert "classification mismatch" in msg


# ── Prometheus counter coverage ──────────────────────────────────────────


def _counter_value(name: str, mode: str, outcome: str) -> float:
    labels = SHADOW_DECISIONS_TOTAL.labels(name=name, mode=mode, outcome=outcome)
    return labels._value.get()  # type: ignore[no-any-return]


async def test_prometheus_counter_increments_per_outcome() -> None:
    _set_mode("readiness", "shadow")
    before_agree = _counter_value("readiness", "shadow", "agree")
    before_disagree = _counter_value("readiness", "shadow", "disagree")

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError

    await decide(legacy=AsyncMock(return_value="A"), candidate=AsyncMock(return_value="A"))
    await decide(legacy=AsyncMock(return_value="A"), candidate=AsyncMock(return_value="B"))

    assert _counter_value("readiness", "shadow", "agree") == before_agree + 1
    assert _counter_value("readiness", "shadow", "disagree") == before_disagree + 1


# ── Unconfigured decision names default to off ──────────────────────────


async def test_unconfigured_name_defaults_to_off_mode() -> None:
    shadow_config.reset_for_tests()

    @shadow_decision("readiness")
    async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> str:
        raise AssertionError

    candidate = AsyncMock(return_value="CAND")
    result = await decide(legacy=AsyncMock(return_value="LEGACY"), candidate=candidate)
    assert result == "LEGACY"
    candidate.assert_not_awaited()
