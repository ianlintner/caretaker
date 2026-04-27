"""Tests for T-A3: LLM-backed CI failure triage.

Covers the new :class:`FailureTriage` schema, the legacy → schema
adapter, the :func:`classify_failure_llm` candidate, and the
``@shadow_decision``-wrapped :func:`triage_failure` dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.github_client.models import CheckConclusion
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.ci_triage import (
    FailureTriage,
    FailureType,
    classify_failure_adapter,
    classify_failure_llm,
    triage_failure,
)
from tests.conftest import make_check_run


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    """Clear ring buffer + active shadow config between tests."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()


def _set_ci_triage_mode(mode: str) -> None:
    cfg = AgenticConfig(ci_triage=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Schema ──────────────────────────────────────────────────────────────


class TestFailureTriageSchema:
    def test_round_trip(self) -> None:
        verdict = FailureTriage(
            category="lint",
            confidence=0.9,
            is_transient=False,
            root_cause_hypothesis="ruff E501 line too long",
            minimal_reproduction="ruff check src",
            suggested_fix="Wrap the offending line at 100 chars.",
            files_to_touch=["src/caretaker/pr_agent/ci_triage.py"],
        )
        dumped = verdict.model_dump_json()
        assert "lint" in dumped
        restored = FailureTriage.model_validate_json(dumped)
        assert restored == verdict

    def test_confidence_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            FailureTriage(
                category="test",
                confidence=-0.1,
                is_transient=False,
                root_cause_hypothesis="x",
                suggested_fix="x",
            )

    def test_confidence_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            FailureTriage(
                category="test",
                confidence=1.5,
                is_transient=False,
                root_cause_hypothesis="x",
                suggested_fix="x",
            )

    def test_category_literal_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            FailureTriage(
                category="not-a-real-category",  # type: ignore[arg-type]
                confidence=0.5,
                is_transient=False,
                root_cause_hypothesis="x",
                suggested_fix="x",
            )

    def test_root_cause_length_limit(self) -> None:
        with pytest.raises(ValidationError):
            FailureTriage(
                category="test",
                confidence=0.5,
                is_transient=False,
                root_cause_hypothesis="x" * 301,
                suggested_fix="x",
            )

    def test_suggested_fix_length_limit(self) -> None:
        with pytest.raises(ValidationError):
            FailureTriage(
                category="test",
                confidence=0.5,
                is_transient=False,
                root_cause_hypothesis="x",
                suggested_fix="x" * 501,
            )

    def test_files_to_touch_default_empty_list(self) -> None:
        verdict = FailureTriage(
            category="test",
            confidence=0.5,
            is_transient=False,
            root_cause_hypothesis="x",
            suggested_fix="x",
        )
        assert verdict.files_to_touch == []
        assert verdict.minimal_reproduction is None


# ── Legacy adapter ──────────────────────────────────────────────────────


class TestClassifyFailureAdapter:
    @pytest.mark.parametrize(
        ("check_name", "expected_category", "expected_transient"),
        [
            ("test-unit", "test", False),
            ("run-pytest", "test", False),
            ("lint", "lint", False),
            ("ruff-check", "lint", False),
            ("eslint-check", "lint", False),
            ("build", "build", False),
            ("mypy", "type", False),
            ("queue-guard", "backpressure", True),
            # Unknown → unknown + not transient (fix expected).
            ("deploy-staging", "unknown", False),
        ],
    )
    def test_category_mapping(
        self,
        check_name: str,
        expected_category: str,
        expected_transient: bool,
    ) -> None:
        cr = make_check_run(name=check_name, conclusion=CheckConclusion.FAILURE)
        verdict = classify_failure_adapter(cr)
        assert verdict.category == expected_category
        assert verdict.is_transient is expected_transient

    def test_timeout_is_transient(self) -> None:
        cr = make_check_run(name="integration", conclusion=CheckConclusion.TIMED_OUT)
        verdict = classify_failure_adapter(cr)
        assert verdict.category == "timeout"
        assert verdict.is_transient is True

    def test_root_cause_includes_matched_pattern(self) -> None:
        cr = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        verdict = classify_failure_adapter(cr)
        assert "Legacy heuristic" in verdict.root_cause_hypothesis
        assert "LINT_FAILURE" in verdict.root_cause_hypothesis

    def test_unknown_has_low_confidence(self) -> None:
        cr = make_check_run(name="deploy-staging", conclusion=CheckConclusion.FAILURE)
        verdict = classify_failure_adapter(cr)
        assert verdict.confidence <= 0.2

    def test_known_has_moderate_confidence(self) -> None:
        cr = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        verdict = classify_failure_adapter(cr)
        # Moderate at best — legacy ladder is cheerfully wrong sometimes.
        assert 0.3 <= verdict.confidence <= 0.7


# ── LLM candidate ───────────────────────────────────────────────────────


class TestClassifyFailureLLM:
    @pytest.mark.asyncio
    async def test_returns_parsed_verdict_on_success(self) -> None:
        verdict = FailureTriage(
            category="lint",
            confidence=0.92,
            is_transient=True,
            root_cause_hypothesis="ruff E501 - cosmetic, re-run after autofix",
            suggested_fix="Run `ruff format`.",
            files_to_touch=["src/foo.py"],
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=verdict)

        cr = make_check_run(
            name="ruff-check",
            conclusion=CheckConclusion.FAILURE,
            output_summary="E501 line too long",
        )
        result = await classify_failure_llm(cr, "E501 line too long", claude=claude)
        assert result is verdict
        assert result is not None
        assert result.category == "lint"
        assert result.is_transient is True

        # Prompt wiring sanity checks — stable prefix in system, log tail in user.
        kwargs = claude.structured_complete.call_args.kwargs
        assert kwargs["feature"] == "ci_triage"
        assert kwargs["schema"] is FailureTriage
        assert "You are a CI failure triager" in kwargs["system"]
        prompt = claude.structured_complete.call_args.args[0]
        assert "ruff-check" in prompt
        assert "E501 line too long" in prompt

    @pytest.mark.asyncio
    async def test_returns_none_on_structured_complete_error(self) -> None:
        claude = MagicMock()
        claude.available = True
        err = StructuredCompleteError(
            raw_text="not json",
            validation_error=ValueError("parse failed"),
        )
        claude.structured_complete = AsyncMock(side_effect=err)

        cr = make_check_run(name="test", conclusion=CheckConclusion.FAILURE)
        result = await classify_failure_llm(cr, "boom", claude=claude)
        assert result is None

    @pytest.mark.asyncio
    async def test_truncates_long_log_tail(self) -> None:
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(
            return_value=FailureTriage(
                category="test",
                confidence=0.5,
                is_transient=False,
                root_cause_hypothesis="x",
                suggested_fix="x",
            )
        )

        big_log = "\n".join(f"line {i}" for i in range(500))
        cr = make_check_run(name="test", conclusion=CheckConclusion.FAILURE)
        await classify_failure_llm(cr, big_log, claude=claude)

        prompt = claude.structured_complete.call_args.args[0]
        # Oldest lines should have been trimmed; newest 200 kept.
        assert "line 0" not in prompt
        assert "line 499" in prompt


# ── Shadow dispatch wiring ──────────────────────────────────────────────


class TestTriageFailureShadow:
    @pytest.mark.asyncio
    async def test_off_mode_uses_legacy_only(self) -> None:
        _set_ci_triage_mode("off")

        cr = make_check_run(name="ruff-check", conclusion=CheckConclusion.FAILURE)
        result = await triage_failure(cr, llm_router=None)

        assert result.failure_type == FailureType.LINT_FAILURE
        assert result.triage is not None
        assert result.triage.category == "lint"
        # Off-mode should not produce any shadow records.
        assert recent_records(name="ci_triage") == []

    @pytest.mark.asyncio
    async def test_shadow_mode_records_disagreement_but_returns_legacy(self) -> None:
        _set_ci_triage_mode("shadow")

        # LLM says flaky/transient; legacy says LINT_FAILURE/not-transient.
        llm_verdict = FailureTriage(
            category="flaky",
            confidence=0.85,
            is_transient=True,
            root_cause_hypothesis="test timed out, ruff was unrelated",
            suggested_fix="Rerun the job.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        cr = make_check_run(
            name="ruff-check",
            conclusion=CheckConclusion.FAILURE,
            output_summary="E501 line too long",
        )
        result = await triage_failure(cr, llm_router=router, repo_slug="ian/demo")

        # Legacy wins in shadow mode.
        assert result.failure_type == FailureType.LINT_FAILURE
        assert result.triage is not None
        assert result.triage.category == "lint"

        records = recent_records(name="ci_triage")
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "disagree"
        assert rec.mode == "shadow"
        assert rec.repo_slug == "ian/demo"
        assert rec.disagreement_reason is not None

    @pytest.mark.asyncio
    async def test_shadow_mode_agrees_when_categories_match(self) -> None:
        _set_ci_triage_mode("shadow")

        # LLM agrees with legacy on category+transience, but uses
        # different free-text fields; the custom comparator should ignore
        # the free-text deltas.
        llm_verdict = FailureTriage(
            category="lint",
            confidence=0.97,
            is_transient=False,
            root_cause_hypothesis="Different wording here",
            suggested_fix="Different suggested fix wording.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        cr = make_check_run(name="ruff-check", conclusion=CheckConclusion.FAILURE)
        await triage_failure(cr, llm_router=router)

        records = recent_records(name="ci_triage")
        assert len(records) == 1
        assert records[0].outcome == "agree"
        assert records[0].disagreement_reason is None

    @pytest.mark.asyncio
    async def test_shadow_mode_swallows_candidate_error(self) -> None:
        _set_ci_triage_mode("shadow")

        claude = MagicMock()
        claude.available = True
        # classify_failure_llm returns None on StructuredCompleteError,
        # which the decorator treats as a legitimate (non-error) return.
        # Make the underlying structured_complete raise a bare exception
        # so we exercise the ``candidate raises`` path of the decorator.
        claude.structured_complete = AsyncMock(side_effect=RuntimeError("boom"))

        router = MagicMock()
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        cr = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        # classify_failure_llm catches StructuredCompleteError but *not*
        # arbitrary runtime errors — so _candidate re-raises and the
        # decorator records a ``candidate_error``. We assert that the
        # hot path still returns the legacy verdict.
        result = await triage_failure(cr, llm_router=router)
        assert result.failure_type == FailureType.LINT_FAILURE

        records = recent_records(name="ci_triage")
        assert len(records) == 1
        assert records[0].outcome == "candidate_error"

    @pytest.mark.asyncio
    async def test_shadow_falls_through_when_structured_complete_fails(self) -> None:
        _set_ci_triage_mode("shadow")

        claude = MagicMock()
        claude.available = True
        err = StructuredCompleteError(
            raw_text="not json",
            validation_error=ValueError("parse failed"),
        )
        claude.structured_complete = AsyncMock(side_effect=err)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        cr = make_check_run(name="lint", conclusion=CheckConclusion.FAILURE)
        result = await triage_failure(cr, llm_router=router)
        assert result.failure_type == FailureType.LINT_FAILURE

        records = recent_records(name="ci_triage")
        # classify_failure_llm swallows StructuredCompleteError and
        # returns None — the decorator compares ``None`` (candidate)
        # against the legacy adapter verdict (FailureTriage) and logs
        # that as a disagreement.
        assert len(records) == 1
        assert records[0].outcome == "disagree"

    @pytest.mark.asyncio
    async def test_enforce_mode_uses_llm_verdict(self) -> None:
        _set_ci_triage_mode("enforce")

        llm_verdict = FailureTriage(
            category="flaky",
            confidence=0.9,
            is_transient=True,
            root_cause_hypothesis="Intermittent network flake.",
            suggested_fix="Rerun the job.",
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)

        router = MagicMock()
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        # A lint-sounding job name; legacy would say LINT_FAILURE,
        # not-transient. In enforce mode the LLM wins.
        cr = make_check_run(name="ruff-check", conclusion=CheckConclusion.FAILURE)
        result = await triage_failure(cr, llm_router=router)

        # ``flaky`` has no legacy row — maps to UNKNOWN.
        assert result.failure_type == FailureType.UNKNOWN
        assert result.triage is not None
        assert result.triage.is_transient is True
        assert result.triage.category == "flaky"

    @pytest.mark.asyncio
    async def test_enforce_falls_back_to_legacy_when_llm_unavailable(self) -> None:
        _set_ci_triage_mode("enforce")

        # No llm_router — _candidate returns None; decorator falls
        # through to legacy.
        cr = make_check_run(name="ruff-check", conclusion=CheckConclusion.FAILURE)
        result = await triage_failure(cr, llm_router=None)
        assert result.failure_type == FailureType.LINT_FAILURE

    @pytest.mark.asyncio
    async def test_off_mode_back_compat_without_llm(self) -> None:
        """T-A3 Part F: backward compat — existing call sites still work."""
        _set_ci_triage_mode("off")

        cr = make_check_run(
            name="test-unit",
            conclusion=CheckConclusion.FAILURE,
            output_summary="FAILED test_foo.py::test_bar - AssertionError",
        )
        result = await triage_failure(cr, llm_router=None)
        assert result.failure_type == FailureType.TEST_FAILURE
        assert result.job_name == "test-unit"
        assert "AssertionError" in result.raw_output


class TestCategoryToFailureTypeMapping:
    """Every legacy regex category has a FailureCategory row (round-trip)."""

    @pytest.mark.parametrize(
        ("legacy", "expected_category"),
        [
            (FailureType.TEST_FAILURE, "test"),
            (FailureType.LINT_FAILURE, "lint"),
            (FailureType.BUILD_FAILURE, "build"),
            (FailureType.TYPE_ERROR, "type"),
            (FailureType.TIMEOUT, "timeout"),
            (FailureType.BACKLOG, "backpressure"),
            (FailureType.UNKNOWN, "unknown"),
        ],
    )
    def test_legacy_roundtrip(
        self,
        legacy: FailureType,
        expected_category: str,
    ) -> None:
        # Build a check_run whose name triggers the legacy pattern, then
        # confirm the adapter yields the expected category.
        name_for: dict[FailureType, tuple[str, Any]] = {
            FailureType.TEST_FAILURE: ("pytest-run", CheckConclusion.FAILURE),
            FailureType.LINT_FAILURE: ("ruff-check", CheckConclusion.FAILURE),
            FailureType.BUILD_FAILURE: ("build-docker", CheckConclusion.FAILURE),
            FailureType.TYPE_ERROR: ("mypy", CheckConclusion.FAILURE),
            FailureType.TIMEOUT: ("integration", CheckConclusion.TIMED_OUT),
            FailureType.BACKLOG: ("queue-guard", CheckConclusion.FAILURE),
            FailureType.UNKNOWN: ("deploy-staging", CheckConclusion.FAILURE),
        }
        name, conclusion = name_for[legacy]
        cr = make_check_run(name=name, conclusion=conclusion)
        verdict = classify_failure_adapter(cr)
        assert verdict.category == expected_category
