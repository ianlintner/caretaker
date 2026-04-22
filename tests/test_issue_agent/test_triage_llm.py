"""Tests for the LLM-backed issue triage candidate (T-A5).

Covers:

* :class:`IssueTriage` schema validation (severity bounds, staleness enum).
* :func:`legacy_to_triage` maps each :class:`IssueClassification` to the
  right kind/severity tuple.
* :func:`select_candidates_by_jaccard` picks overlap-rich candidates and
  excludes self-matches.
* :func:`build_prompt` surfaces the CVE hint when the title mentions one.
* :func:`classify_issue_llm` returns the structured verdict on success,
  ``None`` on :class:`StructuredCompleteError`, and drops hallucinated
  duplicate_of numbers.
* Shadow decorator integration — the three modes (off / shadow / enforce)
  dispatch the legacy + candidate pair correctly through
  :func:`_triage_issue_shadow`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    IssueAgentConfig,
    IssueTriageAgenticConfig,
)
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import clear_records_for_tests, recent_records
from caretaker.github_client.models import Issue, Label, User
from caretaker.issue_agent.agent import IssueAgent, _triage_issue_shadow
from caretaker.issue_agent.classifier import IssueClassification
from caretaker.issue_agent.triage_llm import (
    IssueCandidate,
    IssueTriage,
    build_prompt,
    classify_issue_llm,
    compare_triage,
    legacy_to_triage,
    select_candidates_by_jaccard,
)
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from datetime import datetime


# ── Helpers ──────────────────────────────────────────────────────────────


def make_issue(
    number: int = 1,
    title: str = "Issue",
    body: str = "Body",
    labels: list[str] | None = None,
    updated_at: datetime | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        user=User(login="reporter", id=10, type="User"),
        labels=[Label(name=n) for n in (labels or [])],
        updated_at=updated_at,
    )


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    shadow_config.reset_for_tests()


def _set_issue_triage_mode(mode: str) -> None:
    cfg = AgenticConfig(issue_triage=IssueTriageAgenticConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Schema validation ────────────────────────────────────────────────────


class TestIssueTriageSchema:
    def test_minimal_valid(self) -> None:
        triage = IssueTriage(kind="bug", summary_one_line="parser crashes")
        assert triage.kind == "bug"
        assert triage.staleness == "fresh"
        assert triage.severity is None

    def test_summary_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            IssueTriage(kind="bug", summary_one_line="x" * 151)

    def test_duplicate_confidence_bounds(self) -> None:
        # Valid endpoints.
        IssueTriage(
            kind="bug",
            summary_one_line="s",
            duplicate_of=1,
            duplicate_confidence=0.0,
        )
        IssueTriage(
            kind="bug",
            summary_one_line="s",
            duplicate_of=1,
            duplicate_confidence=1.0,
        )
        with pytest.raises(ValidationError):
            IssueTriage(
                kind="bug",
                summary_one_line="s",
                duplicate_of=1,
                duplicate_confidence=1.01,
            )

    def test_invalid_kind(self) -> None:
        with pytest.raises(ValidationError):
            IssueTriage(kind="urgent", summary_one_line="s")  # type: ignore[arg-type]


# ── Legacy adapter ───────────────────────────────────────────────────────


class TestLegacyAdapter:
    def test_bug_simple_maps_to_minor(self) -> None:
        triage = legacy_to_triage(IssueClassification.BUG_SIMPLE, make_issue(title="crash"))
        assert triage.kind == "bug"
        assert triage.severity == "minor"
        assert triage.staleness == "fresh"

    def test_bug_complex_maps_to_major(self) -> None:
        triage = legacy_to_triage(IssueClassification.BUG_COMPLEX, make_issue(title="huge bug"))
        assert triage.kind == "bug"
        assert triage.severity == "major"

    def test_feature_small_maps_to_feature(self) -> None:
        triage = legacy_to_triage(IssueClassification.FEATURE_SMALL, make_issue(title="add flag"))
        assert triage.kind == "feature"
        assert triage.severity is None

    def test_question_maps_to_question(self) -> None:
        triage = legacy_to_triage(IssueClassification.QUESTION, make_issue(title="how do I?"))
        assert triage.kind == "question"

    def test_stale_propagates_staleness(self) -> None:
        triage = legacy_to_triage(IssueClassification.STALE, make_issue(title="old issue"))
        assert triage.staleness == "stale"

    def test_duplicate_leaves_duplicate_of_none(self) -> None:
        # Legacy hash grouping runs in a separate pass, not per-issue;
        # the adapter cannot invent an issue number.
        triage = legacy_to_triage(IssueClassification.DUPLICATE, make_issue(title="dup of #1"))
        assert triage.duplicate_of is None

    def test_summary_truncated_to_150(self) -> None:
        triage = legacy_to_triage(IssueClassification.BUG_SIMPLE, make_issue(title="x" * 200))
        assert len(triage.summary_one_line) <= 150


# ── Candidate selection ──────────────────────────────────────────────────


class TestJaccardCandidates:
    def test_picks_highest_overlap(self) -> None:
        target = make_issue(1, "login redirect loop", body="infinite redirect when SAML")
        other_good = make_issue(2, "SAML login redirect loop", body="redirect keeps looping")
        other_bad = make_issue(3, "docs update", body="fix typo")
        picks = select_candidates_by_jaccard(target, [other_good, other_bad], limit=5)
        assert [c.number for c in picks] == [2]

    def test_excludes_self(self) -> None:
        target = make_issue(1, "crash on startup", body="crash")
        picks = select_candidates_by_jaccard(target, [target], limit=5)
        assert picks == []

    def test_respects_limit(self) -> None:
        target = make_issue(1, "crash during startup parsing JSON", body="parser crash")
        pool = [
            make_issue(
                n,
                title="crash startup parsing JSON",
                body="parser crash",
            )
            for n in range(2, 10)
        ]
        picks = select_candidates_by_jaccard(target, pool, limit=3)
        assert len(picks) == 3

    def test_zero_limit_returns_empty(self) -> None:
        target = make_issue(1, "x", body="y")
        picks = select_candidates_by_jaccard(target, [make_issue(2)], limit=0)
        assert picks == []


# ── Prompt / CVE pre-filter ──────────────────────────────────────────────


class TestPromptCVEHint:
    def test_cve_in_title_surfaces_hint(self) -> None:
        issue = make_issue(1, title="CVE-2025-1234 in dep foo", body="patch needed")
        prompt = build_prompt(issue, [])
        assert "cve_hint: CVE-2025-1234" in prompt
        assert "share this CVE are duplicate candidates" in prompt

    def test_cve_in_body_surfaces_hint(self) -> None:
        issue = make_issue(1, title="security: upgrade", body="relates to cve-2024-5555")
        prompt = build_prompt(issue, [])
        assert "CVE-2024-5555" in prompt

    def test_no_cve_no_hint(self) -> None:
        issue = make_issue(1, title="normal bug", body="no cve here")
        prompt = build_prompt(issue, [])
        assert "cve_hint" not in prompt

    def test_candidates_rendered(self) -> None:
        issue = make_issue(1, title="t", body="b")
        cands = [IssueCandidate(number=42, title="dup candidate", labels=["bug"])]
        prompt = build_prompt(issue, cands)
        assert "#42" in prompt
        assert "dup candidate" in prompt
        assert "[bug]" in prompt


# ── LLM candidate function ───────────────────────────────────────────────


class TestClassifyIssueLLM:
    async def test_returns_structured_verdict(self) -> None:
        claude = MagicMock()
        verdict = IssueTriage(
            kind="bug",
            severity="major",
            summary_one_line="parser crashes on empty input",
            duplicate_of=42,
            duplicate_confidence=0.9,
        )
        claude.structured_complete = AsyncMock(return_value=verdict)
        issue = make_issue(1, "parser crash", body="throws on empty")
        result = await classify_issue_llm(
            issue,
            candidates=[IssueCandidate(number=42, title="crashes")],
            claude=claude,
        )
        assert result is not None
        assert result.kind == "bug"
        assert result.duplicate_of == 42
        assert result.duplicate_confidence == 0.9
        claude.structured_complete.assert_awaited_once()

    async def test_structured_complete_error_returns_none(self) -> None:
        claude = MagicMock()
        claude.structured_complete = AsyncMock(
            side_effect=StructuredCompleteError(
                raw_text="garbage", validation_error=ValueError("bad")
            )
        )
        result = await classify_issue_llm(
            make_issue(1),
            candidates=[],
            claude=claude,
        )
        assert result is None

    async def test_unexpected_error_returns_none(self) -> None:
        claude = MagicMock()
        claude.structured_complete = AsyncMock(side_effect=RuntimeError("boom"))
        result = await classify_issue_llm(make_issue(1), candidates=[], claude=claude)
        assert result is None

    async def test_hallucinated_duplicate_of_dropped(self) -> None:
        """A duplicate_of number that's not in the candidate set is cleared."""
        claude = MagicMock()
        verdict = IssueTriage(
            kind="bug",
            summary_one_line="crash",
            duplicate_of=99999,  # not in the candidate set
            duplicate_confidence=0.8,
        )
        claude.structured_complete = AsyncMock(return_value=verdict)
        result = await classify_issue_llm(
            make_issue(1, "crash"),
            candidates=[IssueCandidate(number=42, title="other")],
            claude=claude,
        )
        assert result is not None
        assert result.duplicate_of is None
        assert result.duplicate_confidence is None


# ── compare_triage ───────────────────────────────────────────────────────


class TestCompareTriage:
    def test_agrees_on_kind_and_duplicate_of(self) -> None:
        a = IssueTriage(
            kind="bug",
            severity="minor",
            summary_one_line="x",
            suggested_labels=["bug"],
        )
        b = IssueTriage(
            kind="bug",
            severity="major",  # differs but ignored
            summary_one_line="y",  # differs but ignored
            suggested_labels=["bug", "p1"],  # differs but ignored
        )
        assert compare_triage(a, b) is True

    def test_disagrees_on_kind(self) -> None:
        a = IssueTriage(kind="bug", summary_one_line="x")
        b = IssueTriage(kind="feature", summary_one_line="x")
        assert compare_triage(a, b) is False

    def test_disagrees_on_duplicate_of(self) -> None:
        a = IssueTriage(kind="bug", summary_one_line="x", duplicate_of=1)
        b = IssueTriage(kind="bug", summary_one_line="x", duplicate_of=2)
        assert compare_triage(a, b) is False


# ── Shadow decorator integration ────────────────────────────────────────


class TestShadowDecoratorModes:
    async def test_off_mode_returns_legacy_and_skips_candidate(self) -> None:
        _set_issue_triage_mode("off")

        legacy_result = (
            IssueClassification.BUG_SIMPLE,
            IssueTriage(kind="bug", severity="minor", summary_one_line="legacy bug"),
        )
        legacy_fn = AsyncMock(return_value=legacy_result)
        candidate_fn = AsyncMock()

        result = await _triage_issue_shadow(
            legacy=legacy_fn,
            candidate=candidate_fn,
            context={"repo_slug": "o/r", "issue_number": 1},
        )
        assert result == legacy_result
        legacy_fn.assert_awaited_once()
        candidate_fn.assert_not_awaited()
        assert recent_records() == []

    async def test_shadow_disagree_records_and_still_returns_legacy(self) -> None:
        _set_issue_triage_mode("shadow")

        legacy_result = (
            IssueClassification.FEATURE_SMALL,
            IssueTriage(kind="feature", summary_one_line="legacy says feature"),
        )
        candidate_result = (
            IssueClassification.BUG_SIMPLE,
            IssueTriage(kind="bug", severity="minor", summary_one_line="candidate says bug"),
        )
        legacy_fn = AsyncMock(return_value=legacy_result)
        candidate_fn = AsyncMock(return_value=candidate_result)

        result = await _triage_issue_shadow(
            legacy=legacy_fn,
            candidate=candidate_fn,
            context={"repo_slug": "o/r"},
        )

        assert result == legacy_result  # legacy authoritative
        records = recent_records()
        assert len(records) == 1
        assert records[0].outcome == "disagree"
        assert records[0].name == "issue_triage"
        assert records[0].mode == "shadow"

    async def test_enforce_returns_candidate_when_available(self) -> None:
        _set_issue_triage_mode("enforce")

        legacy_result = (
            IssueClassification.FEATURE_SMALL,
            IssueTriage(kind="feature", summary_one_line="legacy"),
        )
        candidate_result = (
            IssueClassification.BUG_SIMPLE,
            IssueTriage(kind="bug", summary_one_line="candidate"),
        )
        legacy_fn = AsyncMock(return_value=legacy_result)
        candidate_fn = AsyncMock(return_value=candidate_result)

        result = await _triage_issue_shadow(
            legacy=legacy_fn,
            candidate=candidate_fn,
        )
        assert result == candidate_result
        candidate_fn.assert_awaited_once()


# ── End-to-end: agent uses legacy verdict in off-mode, enforce in enforce-mode ──


class TestIssueAgentEnforceIntegration:
    async def test_enforce_mode_overrides_classification_when_llm_wired(self) -> None:
        """In enforce mode, the agent honours the LLM verdict even when the legacy
        heuristic would say FEATURE_SMALL.
        """
        _set_issue_triage_mode("enforce")

        issue = make_issue(
            1,
            title="the widget renders weirdly",
            body="please adjust alignment.",
        )
        # Legacy would classify this as FEATURE_SMALL (no bug keywords).
        from caretaker.issue_agent.classifier import classify_issue

        assert classify_issue(issue, IssueAgentConfig()) == IssueClassification.FEATURE_SMALL

        # Fake LLM router that exposes a .claude client with structured_complete.
        llm_router = MagicMock()
        llm_router.claude = MagicMock()
        llm_router.claude.available = True
        llm_router.claude.structured_complete = AsyncMock(
            return_value=IssueTriage(
                kind="bug",
                severity="major",
                summary_one_line="widget render bug",
            )
        )

        github = AsyncMock()
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_assign_bugs=False),  # no dispatch side-effects
            llm_router=llm_router,
        )

        report, tracked = await agent.run({})
        assert report.triaged == 1
        assert tracked[1].classification == IssueClassification.BUG_COMPLEX.value

    async def test_off_mode_ignores_llm_and_uses_legacy(self) -> None:
        _set_issue_triage_mode("off")

        issue = make_issue(1, title="feature request", body="add a new flag")
        llm_router = MagicMock()
        llm_router.claude = MagicMock()
        llm_router.claude.available = True
        llm_router.claude.structured_complete = AsyncMock(
            side_effect=AssertionError("should not be called in off-mode")
        )

        github = AsyncMock()
        github.list_issues.return_value = [issue]
        github.list_pull_requests.return_value = []

        agent = IssueAgent(
            github=github,
            owner="o",
            repo="r",
            config=IssueAgentConfig(auto_assign_features=False),
            llm_router=llm_router,
        )
        report, tracked = await agent.run({})
        assert report.triaged == 1
        # Legacy verdict: FEATURE_SMALL. State machine does not escalate
        # since auto_assign_features=False — issue stays TRIAGED.
        assert tracked[1].classification == IssueClassification.FEATURE_SMALL.value


# ── Agentic config knob ─────────────────────────────────────────────────


class TestIssueTriageAgenticConfig:
    def test_default_pool_size_is_five(self) -> None:
        cfg = IssueTriageAgenticConfig()
        assert cfg.dup_candidate_pool_size == 5
        assert cfg.mode == "off"

    def test_pool_size_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            IssueTriageAgenticConfig(dup_candidate_pool_size=-1)
        with pytest.raises(ValidationError):
            IssueTriageAgenticConfig(dup_candidate_pool_size=51)

    def test_inherits_from_agentic_domain_config(self) -> None:
        # The shadow decorator looks up ``getattr(cfg, name).mode`` on the
        # active AgenticConfig — the subclass must still carry the ``mode``
        # field it inherits from AgenticDomainConfig.
        cfg = IssueTriageAgenticConfig(mode="shadow", dup_candidate_pool_size=10)
        assert isinstance(cfg, AgenticDomainConfig)
        assert cfg.mode == "shadow"
        assert cfg.dup_candidate_pool_size == 10
