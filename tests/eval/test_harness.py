"""Integration tests for :func:`caretaker.eval.harness.run_nightly_eval`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caretaker.eval import store
from caretaker.eval.braintrust_client import BraintrustClient, EvalCase
from caretaker.eval.harness import NightlyReport, run_nightly_eval
from caretaker.eval.scorers import JudgeGrade, LLMJudge
from caretaker.evolution.shadow import (
    ShadowDecisionRecord,
    clear_records_for_tests,
    write_shadow_decision,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Reset the shadow ring buffer + eval store between tests."""
    clear_records_for_tests()
    store.clear_for_tests()
    yield
    clear_records_for_tests()
    store.clear_for_tests()


def _record(
    *,
    name: str,
    legacy_verdict: dict[str, Any],
    candidate_verdict: dict[str, Any] | None,
    outcome: str = "agree",
    minutes_ago: int = 0,
    rid: str = "rec-1",
) -> ShadowDecisionRecord:
    return ShadowDecisionRecord(
        id=rid,
        name=name,
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
    )


# ── Fake Braintrust SDK ──────────────────────────────────────────────────


@dataclass
class _FakeExperiment:
    logged_cases: list[dict[str, Any]] = field(default_factory=list)

    def log(self, **kwargs: Any) -> None:
        self.logged_cases.append(kwargs)

    def summarize(self) -> dict[str, Any]:
        return {"experiment_url": f"https://braintrust.test/exp/{len(self.logged_cases)}"}


@dataclass
class _FakeSDK:
    experiments: list[_FakeExperiment] = field(default_factory=list)
    init_calls: list[dict[str, Any]] = field(default_factory=list)

    def init_experiment(self, **kwargs: Any) -> _FakeExperiment:
        self.init_calls.append(kwargs)
        exp = _FakeExperiment()
        self.experiments.append(exp)
        return exp


# ── Tests ────────────────────────────────────────────────────────────────


class TestRunNightlyEval:
    def test_dry_run_skips_braintrust_and_returns_local_report(self) -> None:
        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "ready"},
            )
        )
        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "blocked"},
                rid="rec-2",
                outcome="disagree",
            )
        )

        since = datetime.now(UTC) - timedelta(hours=1)
        report = run_nightly_eval(
            since=since,
            sites=["readiness"],
            dry_run=True,
        )

        assert isinstance(report, NightlyReport)
        readiness = report.site("readiness")
        assert readiness is not None
        assert readiness.record_count == 2
        # One agree + one disagree → 0.5 mean
        assert readiness.agreement_rate() == pytest.approx(0.5)
        assert readiness.braintrust_logged is False

    def test_registers_prometheus_gauge_per_scorer(self) -> None:
        from caretaker.eval.harness import EVAL_AGREEMENT_RATE

        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "ready"},
            )
        )
        run_nightly_eval(
            since=datetime.now(UTC) - timedelta(hours=1),
            sites=["readiness"],
            dry_run=True,
        )
        value = EVAL_AGREEMENT_RATE.labels(
            site="readiness",
            scorer="readiness_verdict_match",
        )._value.get()
        assert value == 1.0

    def test_rejects_unknown_site(self) -> None:
        with pytest.raises(ValueError, match="unknown eval sites"):
            run_nightly_eval(
                since=datetime.now(UTC) - timedelta(hours=1),
                sites=["not_a_real_site"],
                dry_run=True,
            )

    def test_rejects_inverted_window(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="must precede"):
            run_nightly_eval(since=now, until=now - timedelta(hours=1), dry_run=True)

    def test_injected_record_loader_bypasses_ring_buffer(self) -> None:
        loaded: list[str] = []

        def _loader(site: str, since: datetime, until: datetime) -> list[ShadowDecisionRecord]:
            loaded.append(site)
            return [
                _record(
                    name=site,
                    legacy_verdict={"verdict": "ready"},
                    candidate_verdict={"verdict": "ready"},
                )
            ]

        report = run_nightly_eval(
            since=datetime.now(UTC) - timedelta(hours=1),
            sites=["readiness"],
            dry_run=True,
            record_loader=_loader,
        )
        assert loaded == ["readiness"]
        site_report = report.site("readiness")
        assert site_report is not None
        assert site_report.record_count == 1

    def test_fake_braintrust_client_uploads_experiment_per_site(self) -> None:
        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "ready"},
            )
        )

        sdk = _FakeSDK()
        client = BraintrustClient(sdk=sdk, api_key="test-key")
        report = run_nightly_eval(
            since=datetime.now(UTC) - timedelta(hours=1),
            sites=["readiness"],
            braintrust_client=client,
        )
        readiness = report.site("readiness")
        assert readiness is not None
        assert readiness.braintrust_logged is True
        assert readiness.experiment_url is not None
        assert len(sdk.init_calls) == 1
        assert sdk.init_calls[0]["name"].startswith("readiness-")
        assert len(sdk.experiments[0].logged_cases) == 1

    def test_report_is_mirrored_into_local_store(self) -> None:
        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready"},
                candidate_verdict={"verdict": "ready"},
            )
        )
        run_nightly_eval(
            since=datetime.now(UTC) - timedelta(hours=1),
            sites=["readiness"],
            dry_run=True,
        )
        latest = store.latest_report()
        assert latest is not None
        assert latest.site("readiness") is not None
        # Rolling 7d mean exists after the first run
        assert store.rolling_agreement_rate("readiness") == pytest.approx(1.0)

    def test_llm_judge_runs_on_readiness_only(self) -> None:
        write_shadow_decision(
            _record(
                name="readiness",
                legacy_verdict={"verdict": "ready", "summary": "looks good"},
                candidate_verdict={"verdict": "ready", "summary": "solid"},
            )
        )
        judge_calls: list[str] = []

        def _judge(prompt: str) -> JudgeGrade:
            judge_calls.append(prompt)
            return JudgeGrade(score=0.9, rationale="")

        llm_judge = LLMJudge(
            judge=_judge,
            judge_model="opus-4.7",
            candidate_model="gpt-4o",
        )
        report = run_nightly_eval(
            since=datetime.now(UTC) - timedelta(hours=1),
            sites=["readiness"],
            dry_run=True,
            llm_judge=llm_judge,
        )
        readiness = report.site("readiness")
        assert readiness is not None
        scorer_names = [s.scorer for s in readiness.scorer_summaries]
        assert "llm_judge_readiness_quality" in scorer_names
        assert len(judge_calls) == 1


# ── Braintrust fail-open ─────────────────────────────────────────────────


class TestBraintrustFailOpen:
    def test_no_api_key_no_sdk_is_local_only(self) -> None:
        client = BraintrustClient(sdk=None, api_key=None)
        assert client.available is False
        result = client.log_experiment(
            "readiness-test",
            [
                EvalCase(
                    input={"x": 1},
                    expected={"y": 1},
                    actual={"y": 1},
                    scores={"s": 1.0},
                )
            ],
        )
        assert result.logged is False
        assert result.experiment_url is None

    def test_sdk_without_key_is_unavailable(self) -> None:
        client = BraintrustClient(sdk=_FakeSDK(), api_key=None)
        assert client.available is False

    def test_score_coercion_clamps_bools_and_numbers(self) -> None:
        sdk = _FakeSDK()
        client = BraintrustClient(sdk=sdk, api_key="k")
        client.log_experiment(
            "x",
            [
                EvalCase(
                    input={},
                    expected={},
                    actual={},
                    scores={"a": True, "b": False, "c": 1.5, "d": -0.3},
                )
            ],
        )
        logged = sdk.experiments[0].logged_cases[0]["scores"]
        assert logged == {"a": 1.0, "b": 0.0, "c": 1.0, "d": 0.0}
