"""Unit tests for :mod:`caretaker.eval.scorers`."""

from __future__ import annotations

import json

import pytest

from caretaker.eval.scorers import (
    DEFAULT_SCORER_REGISTRY,
    JudgeGrade,
    LLMJudge,
    ScorerResult,
    bot_identity_match,
    cascade_action_match,
    ci_triage_category_match,
    crystallizer_category_match,
    dispatch_guard_match,
    executor_routing_match,
    issue_triage_kind_match,
    readiness_verdict_match,
    review_classification_match,
    stuck_pr_match,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _j(**kwargs: object) -> str:
    return json.dumps(kwargs, sort_keys=True)


# ── Readiness ────────────────────────────────────────────────────────────


class TestReadinessVerdictMatch:
    def test_agree(self) -> None:
        r = readiness_verdict_match(_j(verdict="ready"), _j(verdict="ready"))
        assert r.score == 1.0
        assert r.reason is None

    def test_disagree(self) -> None:
        r = readiness_verdict_match(_j(verdict="ready"), _j(verdict="blocked"))
        assert r.score == 0.0
        assert r.reason is not None
        assert "verdict" in r.reason

    def test_candidate_missing_yields_error_row(self) -> None:
        r = readiness_verdict_match(_j(verdict="ready"), None)
        assert r.score == 0.0
        assert r.metadata["candidate_error"] is True


# ── CI triage ────────────────────────────────────────────────────────────


class TestCITriage:
    def test_agree_on_both_fields(self) -> None:
        r = ci_triage_category_match(
            _j(category="flake", is_transient=True),
            _j(category="flake", is_transient=True),
        )
        assert r.score == 1.0

    def test_disagrees_when_transient_flag_flips(self) -> None:
        r = ci_triage_category_match(
            _j(category="flake", is_transient=True),
            _j(category="flake", is_transient=False),
        )
        assert r.score == 0.0
        assert r.reason is not None
        assert "is_transient" in r.reason


# ── Issue triage ─────────────────────────────────────────────────────────


class TestIssueTriage:
    def test_identical_labels(self) -> None:
        labels = ["bug", "ci"]
        r = issue_triage_kind_match(
            _j(kind="bug_report", suggested_labels=labels),
            _j(kind="bug_report", suggested_labels=labels),
        )
        assert r.score == 1.0
        assert r.metadata["suggested_labels_cosine"] == pytest.approx(1.0)

    def test_cosine_above_threshold(self) -> None:
        r = issue_triage_kind_match(
            _j(kind="bug_report", suggested_labels=["bug", "ci", "flaky"]),
            _j(kind="bug_report", suggested_labels=["bug", "ci", "flaky", "retry"]),
        )
        # cos(3 common / sqrt(3)·sqrt(4)) ≈ 3 / (sqrt(3) * 2) ≈ 0.866 > 0.8
        assert r.score == 1.0

    def test_cosine_below_threshold(self) -> None:
        r = issue_triage_kind_match(
            _j(kind="bug_report", suggested_labels=["bug"]),
            _j(kind="bug_report", suggested_labels=["feature", "docs", "chore"]),
        )
        assert r.score == 0.0
        assert r.reason is not None
        assert "cosine" in r.reason

    def test_disagreeing_kind_shortcircuits(self) -> None:
        r = issue_triage_kind_match(
            _j(kind="bug_report", suggested_labels=["x"]),
            _j(kind="feature_request", suggested_labels=["x"]),
        )
        assert r.score == 0.0
        assert r.reason is not None
        assert r.reason.startswith("kind:")


# ── Remaining exact-match scorers ────────────────────────────────────────


def test_dispatch_guard_match() -> None:
    agree = dispatch_guard_match(
        _j(is_self_echo=False, is_human_intent=True),
        _j(is_self_echo=False, is_human_intent=True),
    )
    assert agree.score == 1.0
    disagree = dispatch_guard_match(
        _j(is_self_echo=False, is_human_intent=True),
        _j(is_self_echo=True, is_human_intent=True),
    )
    assert disagree.score == 0.0
    assert disagree.reason is not None
    assert "is_self_echo" in disagree.reason


def test_review_classification_match() -> None:
    r = review_classification_match(
        _j(kind="nit", severity="low"),
        _j(kind="nit", severity="low"),
    )
    assert r.score == 1.0


def test_cascade_action_match() -> None:
    r = cascade_action_match(_j(action="close"), _j(action="close"))
    assert r.score == 1.0
    r2 = cascade_action_match(_j(action="close"), _j(action="ignore"))
    assert r2.score == 0.0


def test_stuck_pr_match() -> None:
    r = stuck_pr_match(
        _j(is_stuck=True, recommended_action="rebase"),
        _j(is_stuck=True, recommended_action="rebase"),
    )
    assert r.score == 1.0
    r2 = stuck_pr_match(
        _j(is_stuck=True, recommended_action="rebase"),
        _j(is_stuck=True, recommended_action="close"),
    )
    assert r2.score == 0.0


def test_bot_identity_match() -> None:
    r = bot_identity_match(_j(is_automated=True), _j(is_automated=True))
    assert r.score == 1.0
    r2 = bot_identity_match(_j(is_automated=True), _j(is_automated=False))
    assert r2.score == 0.0


def test_executor_routing_match_on_path_field() -> None:
    r = executor_routing_match(_j(path="copilot"), _j(path="copilot"))
    assert r.score == 1.0


def test_executor_routing_match_on_scalar_verdict() -> None:
    # The scorer also accepts raw scalar verdicts wrapped in ``__value__``.
    r = executor_routing_match('"copilot"', '"copilot"')
    assert r.score == 1.0
    r2 = executor_routing_match('"copilot"', '"foundry"')
    assert r2.score == 0.0


def test_crystallizer_category_match() -> None:
    r = crystallizer_category_match(_j(category="refactor"), _j(category="refactor"))
    assert r.score == 1.0


# ── Malformed input handling ─────────────────────────────────────────────


def test_malformed_json_degrades_gracefully() -> None:
    r = readiness_verdict_match("not json at all", '{"verdict": "ready"}')
    assert r.score == 0.0
    assert r.reason is not None
    assert "not JSON" in r.reason


# ── Registry completeness ────────────────────────────────────────────────


def test_registry_covers_every_decision_site() -> None:
    expected = {
        "readiness",
        "ci_triage",
        "issue_triage",
        "dispatch_guard",
        "review_classification",
        "cascade",
        "stuck_pr",
        "bot_identity",
        "executor_routing",
        "crystallizer_category",
    }
    assert set(DEFAULT_SCORER_REGISTRY.keys()) == expected
    for scorers in DEFAULT_SCORER_REGISTRY.values():
        assert len(scorers) >= 1


# ── Result clamping ──────────────────────────────────────────────────────


def test_scorer_result_clamps_score() -> None:
    assert ScorerResult(score=-1.0).score == 0.0
    assert ScorerResult(score=2.5).score == 1.0
    assert ScorerResult(score=float("nan")).score == 0.0


# ── LLM judge ────────────────────────────────────────────────────────────


class TestLLMJudge:
    def test_requires_different_judge_and_candidate_models(self) -> None:
        with pytest.raises(ValueError, match="must differ"):
            LLMJudge(
                judge=lambda _: JudgeGrade(score=1.0, rationale=""),
                judge_model="same",
                candidate_model="same",
            )

    def test_scripted_passing_grade(self) -> None:
        judge = LLMJudge(
            judge=lambda _: JudgeGrade(score=0.95, rationale="faithful"),
            judge_model="opus-4.7",
            candidate_model="gpt-4o",
        )
        r = judge(
            _j(summary="CI is failing on frontend tests."),
            _j(summary="The frontend test suite is broken in CI."),
        )
        assert r.score == 1.0
        assert r.metadata["judge_model"] == "opus-4.7"
        assert r.metadata["candidate_model"] == "gpt-4o"

    def test_scripted_failing_grade_surfaces_rationale(self) -> None:
        judge = LLMJudge(
            judge=lambda _: JudgeGrade(score=0.2, rationale="hallucinated blocker"),
            judge_model="opus-4.7",
            candidate_model="gpt-4o",
        )
        r = judge(
            _j(summary="Ready to merge."),
            _j(summary="The PR is blocked by a CI failure that did not happen."),
        )
        assert r.score == 0.0
        assert r.reason == "hallucinated blocker"

    def test_judge_exception_fails_closed_with_metadata(self) -> None:
        def _boom(_: str) -> JudgeGrade:
            raise RuntimeError("network down")

        judge = LLMJudge(
            judge=_boom,
            judge_model="opus-4.7",
            candidate_model="gpt-4o",
        )
        r = judge(_j(summary="a"), _j(summary="b"))
        assert r.score == 0.0
        assert r.metadata["judge_error"] is True

    def test_candidate_error_shortcircuits_judge_call(self) -> None:
        calls: list[str] = []

        def _judge(prompt: str) -> JudgeGrade:
            calls.append(prompt)
            return JudgeGrade(score=1.0, rationale="")

        judge = LLMJudge(
            judge=_judge,
            judge_model="opus-4.7",
            candidate_model="gpt-4o",
        )
        r = judge(_j(summary="x"), None)
        assert r.score == 0.0
        assert r.metadata["candidate_error"] is True
        assert calls == []
