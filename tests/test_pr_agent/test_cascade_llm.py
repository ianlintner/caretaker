"""Tests for the Phase 2 LLM-backed cascade redirection/close migration (T-A6).

Covers every call-out in the T-A6 task:

* ``CascadeDecision`` schema validation.
* Legacy adapter for each rule (redirect, close_pr, keep_open).
* LLM candidate happy path + ``StructuredCompleteError`` returns None.
* 3-mode shadow integration (off / shadow / enforce) for both the
  redirect decision and the close-PR decision.
* Regression assertions that :func:`parse_linked_issues` stays regex-
  driven and unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.github_client.models import Issue, User
from caretaker.graph import writer as graph_writer
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.cascade import CascadeKind, parse_linked_issues
from caretaker.pr_agent.cascade_llm import (
    CascadeDecision,
    CascadeEventContext,
    build_cascade_prompt,
    cascade_decision_from_close_pr_rule,
    cascade_decision_from_keep_open_rule,
    cascade_decision_from_redirect_rule,
    decide_cascade_llm,
    plan_on_issue_closed_as_duplicate,
)
from caretaker.state.models import TrackedPR

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    graph_writer.reset_for_tests()


def _set_cascade_mode(mode: str) -> None:
    cfg = AgenticConfig(cascade=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


def _issue(number: int) -> Issue:
    return Issue(
        number=number,
        title=f"issue {number}",
        body="",
        state="closed",
        user=User(login="bot", id=1, type="Bot"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ── Schema ───────────────────────────────────────────────────────────────


class TestCascadeDecisionSchema:
    def test_happy_path_parses(self) -> None:
        d = CascadeDecision(action="redirect", justification="j", confidence=0.7)
        assert d.action == "redirect"
        assert d.confidence == 0.7

    def test_action_literal_closed_enum(self) -> None:
        with pytest.raises(ValidationError):
            CascadeDecision(
                action="banana",  # type: ignore[arg-type]
                justification="j",
                confidence=0.5,
            )

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CascadeDecision(action="redirect", justification="j", confidence=1.5)
        with pytest.raises(ValidationError):
            CascadeDecision(action="redirect", justification="j", confidence=-0.1)

    def test_justification_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            CascadeDecision(action="redirect", justification="x" * 301, confidence=0.5)

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            CascadeDecision(action="redirect", confidence=0.5)  # type: ignore[call-arg]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CascadeDecision(
                action="redirect",
                justification="j",
                confidence=0.5,
                bogus="x",  # type: ignore[call-arg]
            )


# ── Legacy adapters ──────────────────────────────────────────────────────


class TestLegacyAdapters:
    def test_redirect_adapter(self) -> None:
        d = cascade_decision_from_redirect_rule(canonical_issue_number=1, closed_issue_number=5)
        assert d.action == "redirect"
        assert d.confidence == 1.0
        assert "Legacy rule" in d.justification
        assert "#1" in d.justification
        assert "#5" in d.justification

    def test_close_pr_adapter(self) -> None:
        d = cascade_decision_from_close_pr_rule(canonical_issue_number=1, closed_issue_number=5)
        assert d.action == "close_pr"
        assert d.confidence == 1.0
        assert "Legacy rule" in d.justification

    def test_keep_open_adapter(self) -> None:
        d = cascade_decision_from_keep_open_rule(pr_number=42, closed_issue_number=5)
        assert d.action == "keep_open"
        assert d.confidence == 1.0
        assert "Legacy rule" in d.justification
        assert "#42" in d.justification


# ── LLM candidate ────────────────────────────────────────────────────────


class TestDecideCascadeLLM:
    def test_prompt_contains_required_payload(self) -> None:
        ctx = CascadeEventContext(
            event_type="on_issue_closed_as_duplicate",
            pr_number=200,
            pr_title="Wire up new feature",
            pr_body="Adds X. Fixes #5",
            linked_issues=[5],
            closed_issue_number=5,
            canonical_issue_number=1,
            repo_slug="ian/demo",
        )
        prompt = build_cascade_prompt(ctx)
        assert "on_issue_closed_as_duplicate" in prompt
        assert "#200" in prompt
        assert "Wire up new feature" in prompt
        assert "Adds X. Fixes #5" in prompt
        assert "ian/demo" in prompt
        assert "#5" in prompt
        assert "#1" in prompt

    async def test_happy_path_returns_schema_instance(self) -> None:
        fake_verdict = CascadeDecision(
            action="redirect", justification="llm says redirect", confidence=0.8
        )

        class _FakeClaude:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def structured_complete(
                self,
                prompt: str,
                *,
                schema: type,
                feature: str,
                system: str | None = None,
            ) -> Any:
                self.calls.append(
                    {
                        "prompt": prompt,
                        "schema": schema,
                        "feature": feature,
                        "system": system,
                    }
                )
                return fake_verdict

        claude = _FakeClaude()
        ctx = CascadeEventContext(
            event_type="on_issue_closed_as_duplicate",
            pr_number=7,
            repo_slug="ian/demo",
            closed_issue_number=5,
            canonical_issue_number=1,
        )
        result = await decide_cascade_llm(ctx, claude=claude)  # type: ignore[arg-type]
        assert result is fake_verdict
        assert len(claude.calls) == 1
        call = claude.calls[0]
        assert call["feature"] == "cascade_decision"
        assert call["schema"] is CascadeDecision
        assert call["system"] is not None
        assert "cascade planner" in call["system"]
        assert "#7" in call["prompt"]

    async def test_structured_complete_error_returns_none(self) -> None:
        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="not-json", validation_error=ValueError("bad")
        )
        ctx = CascadeEventContext(
            event_type="on_issue_closed_as_duplicate",
            pr_number=9,
            closed_issue_number=5,
            canonical_issue_number=1,
        )
        result = await decide_cascade_llm(ctx, claude=claude)
        assert result is None


# ── Shadow-mode integration: off / shadow / enforce ──────────────────────


class TestOffMode:
    async def test_short_body_close_pr_matches_legacy(self) -> None:
        _set_cascade_mode("off")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}

        # Candidate should never be consulted — use a claude stub that
        # raises to prove it.
        claude = AsyncMock()
        claude.structured_complete.side_effect = AssertionError("candidate was called")

        actions = await plan_on_issue_closed_as_duplicate(
            issue, 1, tracked, bodies, claude=claude, repo_slug="o/r"
        )
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds
        assert recent_records() == []

    async def test_long_body_no_close(self) -> None:
        _set_cascade_mode("off")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        long_body = "Fixes #5\n\n" + "real implementation text " * 20
        bodies = {200: long_body}

        actions = await plan_on_issue_closed_as_duplicate(
            issue, 1, tracked, bodies, claude=None, repo_slug="o/r"
        )
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR not in kinds

    async def test_unrelated_pr_skipped(self) -> None:
        _set_cascade_mode("off")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Unrelated PR body"}
        actions = await plan_on_issue_closed_as_duplicate(issue, 1, tracked, bodies, claude=None)
        assert actions == []


class TestShadowMode:
    async def test_shadow_records_disagreement_on_close(self) -> None:
        _set_cascade_mode("shadow")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}  # legacy says close_pr

        # LLM disagrees: thinks we should keep_open for close gate and
        # redirect for redirect gate.
        verdicts = [
            CascadeDecision(action="redirect", justification="llm redirect", confidence=0.8),
            CascadeDecision(action="keep_open", justification="llm keep", confidence=0.6),
        ]
        claude = AsyncMock()
        claude.structured_complete.side_effect = verdicts

        actions = await plan_on_issue_closed_as_duplicate(
            issue, 1, tracked, bodies, claude=claude, repo_slug="o/r"
        )
        kinds = [a.kind for a in actions]
        # Legacy still authoritative -> redirect comment + close_pr emitted.
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds
        records = recent_records(name="cascade")
        # One record per decision gate. Redirect gate: both say redirect -> agree.
        # Close gate: legacy says close_pr, candidate says keep_open -> disagree.
        outcomes = sorted(r.outcome for r in records)
        assert outcomes == ["agree", "disagree"]
        assert all(r.name == "cascade" for r in records)
        assert all(r.repo_slug == "o/r" for r in records)

    async def test_shadow_candidate_error_recorded(self) -> None:
        _set_cascade_mode("shadow")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}

        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="x", validation_error=ValueError("bad")
        )

        actions = await plan_on_issue_closed_as_duplicate(issue, 1, tracked, bodies, claude=claude)
        kinds = [a.kind for a in actions]
        # Legacy authoritative; candidate returns None (treated as
        # disagreement under default equality compare).
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds
        records = recent_records(name="cascade")
        # Both decision gates produced records (disagreement because
        # the action-level compare sees a CascadeDecision vs None).
        assert len(records) == 2


class TestEnforceMode:
    async def test_enforce_candidate_wins(self) -> None:
        _set_cascade_mode("enforce")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}  # legacy would say close_pr

        # Candidate says redirect for gate 1, keep_open for gate 2 ->
        # no CLOSE_PR emitted.
        verdicts = [
            CascadeDecision(action="redirect", justification="llm redirect", confidence=0.9),
            CascadeDecision(action="keep_open", justification="llm keep", confidence=0.8),
        ]
        claude = AsyncMock()
        claude.structured_complete.side_effect = verdicts

        actions = await plan_on_issue_closed_as_duplicate(issue, 1, tracked, bodies, claude=claude)
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR not in kinds

    async def test_enforce_candidate_error_falls_through_to_legacy(self) -> None:
        _set_cascade_mode("enforce")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}

        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="x", validation_error=ValueError("bad")
        )

        actions = await plan_on_issue_closed_as_duplicate(issue, 1, tracked, bodies, claude=claude)
        kinds = [a.kind for a in actions]
        # Legacy fallback -> both comment + close emitted.
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds

    async def test_enforce_no_claude_falls_through(self) -> None:
        _set_cascade_mode("enforce")
        issue = _issue(5)
        tracked = {200: TrackedPR(number=200)}
        bodies = {200: "Fixes #5"}

        # No claude client -> candidate returns None -> legacy wins.
        actions = await plan_on_issue_closed_as_duplicate(issue, 1, tracked, bodies, claude=None)
        kinds = [a.kind for a in actions]
        assert CascadeKind.COMMENT_ON_PR in kinds
        assert CascadeKind.CLOSE_PR in kinds


# ── Regression: parse_linked_issues unchanged ────────────────────────────


class TestParseLinkedIssuesUnchanged:
    """The deterministic regex parser must not be touched by T-A6."""

    def test_fixes_keyword(self) -> None:
        assert parse_linked_issues("Fixes #42") == [42]

    def test_closes_plural_variants(self) -> None:
        body = "Resolves #1. Closed #2. Fix #3. resolves #4"
        assert parse_linked_issues(body) == [1, 2, 3, 4]

    def test_deduplicates(self) -> None:
        assert parse_linked_issues("Fixes #7 and also closes #7") == [7]

    def test_no_matches(self) -> None:
        assert parse_linked_issues("Just some description text") == []

    def test_empty(self) -> None:
        assert parse_linked_issues("") == []

    def test_unknown_keyword_ignored(self) -> None:
        # "mentions #7" is not a closing keyword.
        assert parse_linked_issues("This PR mentions #7 for context") == []
