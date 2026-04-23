"""Per-``candidate_model`` break-out tests for the nightly eval harness.

The PR #503 follow-up adds :attr:`SiteReport.per_model_reports` so an
operator running two models against the same legacy heuristic (one with
``agentic.<site>.model_override`` set, one without) can see a per-model
agreement rate in the nightly report — not just the window-wide mean
that would average the two models together and hide a regression.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caretaker.eval import store
from caretaker.eval.harness import run_nightly_eval
from caretaker.evolution.shadow import (
    ShadowDecisionRecord,
    clear_records_for_tests,
    write_shadow_decision,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    clear_records_for_tests()
    store.clear_for_tests()
    yield
    clear_records_for_tests()
    store.clear_for_tests()


def _record(
    *,
    rid: str,
    candidate_model: str | None,
    legacy_verdict: dict[str, Any],
    candidate_verdict: dict[str, Any] | None,
    outcome: str = "agree",
    minutes_ago: int = 0,
) -> ShadowDecisionRecord:
    return ShadowDecisionRecord(
        id=rid,
        name="readiness",
        repo_slug="ian/demo",
        run_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        outcome=outcome,  # type: ignore[arg-type]
        mode="shadow",
        legacy_verdict_json=json.dumps(legacy_verdict, sort_keys=True),
        candidate_verdict_json=(
            json.dumps(candidate_verdict, sort_keys=True) if candidate_verdict is not None else None
        ),
        disagreement_reason=None,
        context_json='{"repo_slug": "ian/demo"}',
        legacy_model="claude-sonnet-4-5",
        candidate_model=candidate_model,
    )


def test_two_candidate_models_surface_per_model_breakout() -> None:
    """Two models against the same legacy heuristic → two per-model rows.

    Model A agrees twice; model B disagrees once and agrees once. The
    site-level mean blends the two (so you can't tell which model
    regressed), but the per-model breakout should clearly show B's
    lower rate.
    """
    # Model A — both agree.
    write_shadow_decision(
        _record(
            rid="a1",
            candidate_model="azure_ai/claude-sonnet-4",
            legacy_verdict={"verdict": "ready"},
            candidate_verdict={"verdict": "ready"},
            outcome="agree",
        )
    )
    write_shadow_decision(
        _record(
            rid="a2",
            candidate_model="azure_ai/claude-sonnet-4",
            legacy_verdict={"verdict": "blocked"},
            candidate_verdict={"verdict": "blocked"},
            outcome="agree",
        )
    )
    # Model B — one agree, one disagree.
    write_shadow_decision(
        _record(
            rid="b1",
            candidate_model="azure_ai/gpt-5",
            legacy_verdict={"verdict": "ready"},
            candidate_verdict={"verdict": "ready"},
            outcome="agree",
        )
    )
    write_shadow_decision(
        _record(
            rid="b2",
            candidate_model="azure_ai/gpt-5",
            legacy_verdict={"verdict": "blocked"},
            candidate_verdict={"verdict": "ready"},
            outcome="disagree",
        )
    )

    since = datetime.now(UTC) - timedelta(hours=1)
    report = run_nightly_eval(since=since, sites=["readiness"], dry_run=True)

    site = report.site("readiness")
    assert site is not None
    assert site.record_count == 4
    # With a mix of models the site-wide ``candidate_model`` attribute
    # reduces to ``None`` — the per-model breakout is where the detail
    # lives.
    assert site.candidate_model is None

    models = {pm.candidate_model: pm for pm in site.per_model_reports}
    assert set(models) == {"azure_ai/claude-sonnet-4", "azure_ai/gpt-5"}

    sonnet = models["azure_ai/claude-sonnet-4"]
    gpt5 = models["azure_ai/gpt-5"]
    assert sonnet.record_count == 2
    assert gpt5.record_count == 2
    # Sonnet agreed 2/2, gpt5 1/2.
    assert sonnet.agreement_rate() == pytest.approx(1.0)
    assert gpt5.agreement_rate() < sonnet.agreement_rate()


def test_single_model_window_produces_empty_breakout() -> None:
    """When every record shares a model, the breakout list stays empty.

    Keeps the report JSON clean for the common case — operators who
    haven't set ``model_override`` shouldn't see a single-entry list
    that duplicates the site-level scorer summary.
    """
    for i in range(3):
        write_shadow_decision(
            _record(
                rid=f"r{i}",
                candidate_model="claude-sonnet-4-5",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "ready"},
                outcome="agree",
            )
        )

    since = datetime.now(UTC) - timedelta(hours=1)
    report = run_nightly_eval(since=since, sites=["readiness"], dry_run=True)

    site = report.site("readiness")
    assert site is not None
    assert site.per_model_reports == []
    assert site.candidate_model == "claude-sonnet-4-5"


def test_site_report_json_includes_candidate_model_fields() -> None:
    """``SiteReport.to_dict`` carries the new fields for the report JSON."""
    write_shadow_decision(
        _record(
            rid="x1",
            candidate_model="azure_ai/claude-sonnet-4",
            legacy_verdict={"verdict": "ready"},
            candidate_verdict={"verdict": "ready"},
            outcome="agree",
        )
    )
    write_shadow_decision(
        _record(
            rid="x2",
            candidate_model="azure_ai/gpt-5",
            legacy_verdict={"verdict": "ready"},
            candidate_verdict={"verdict": "blocked"},
            outcome="disagree",
        )
    )

    since = datetime.now(UTC) - timedelta(hours=1)
    report = run_nightly_eval(since=since, sites=["readiness"], dry_run=True)
    payload = report.to_dict()
    site_payload = next(s for s in payload["sites"] if s["site"] == "readiness")

    assert site_payload["candidate_model"] is None  # mixed-model window
    breakout_models = {pm["candidate_model"] for pm in site_payload["per_model_reports"]}
    assert breakout_models == {"azure_ai/claude-sonnet-4", "azure_ai/gpt-5"}
    # Every per-model entry surfaces its own agreement rate.
    for pm in site_payload["per_model_reports"]:
        assert "agreement_rate" in pm
        assert "record_count" in pm
