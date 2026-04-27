"""Tests for T-A8: LLM-backed stuck-PR detection migration.

Covers every call-out in T-A8:

* :class:`StuckVerdict` schema validation.
* Legacy adapter mapping: binary age-heuristic -> stuck/not-stuck.
* LLM candidate prompt payload + happy path + ``StructuredCompleteError``
  short-circuit to ``None``.
* Minimum-age pre-filter: PRs younger than ``stuck_age_hours`` skip
  both paths entirely.
* Shadow dispatch wiring in all three modes.
* Solo-repo special case: when readiness says ``ready`` and
  ``collaborator_count`` is 1, the candidate returns
  ``solo_repo_no_reviewer`` / ``self_approve_on_solo``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    CIConfig,
    CopilotConfig,
    OwnershipConfig,
    PRAgentConfig,
    ReadinessConfig,
)
from caretaker.evolution import shadow_config
from caretaker.evolution.shadow import (
    clear_records_for_tests,
    recent_records,
)
from caretaker.github_client.models import (
    CheckConclusion,
    Label,
    ReviewState,
    User,
)
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_agent.agent import PRAgent, PRAgentReport
from caretaker.pr_agent.readiness_llm import Readiness
from caretaker.pr_agent.stuck_pr_llm import (
    PRStuckContext,
    StuckVerdict,
    build_stuck_pr_prompt,
    evaluate_stuck_pr_llm,
    stuck_from_legacy,
)
from caretaker.state.models import PRTrackingState, TrackedPR
from tests.conftest import make_check_run, make_pr, make_review


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    """Clear ring buffer + active shadow config between tests."""
    clear_records_for_tests()
    shadow_config.reset_for_tests()


def _set_stuck_pr_mode(mode: str) -> None:
    """Install an :class:`AgenticConfig` with the stuck-PR mode set."""
    cfg = AgenticConfig(stuck_pr=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


def _make_pr_config(*, stuck_age_hours: int = 24, required_reviews: int = 1) -> PRAgentConfig:
    config = PRAgentConfig()
    config.ci = CIConfig()
    config.copilot = CopilotConfig(max_retries=2)
    config.ownership = OwnershipConfig()
    config.readiness = ReadinessConfig(required_reviews=required_reviews)
    config.stuck_age_hours = stuck_age_hours
    return config


# ── Part 1: Schema validation ────────────────────────────────────────────


class TestStuckVerdictSchema:
    def test_happy_path(self) -> None:
        verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="abandoned",
            recommended_action="escalate",
            explanation="PR open 10 days with no review",
            confidence=0.8,
        )
        assert verdict.is_stuck is True
        assert verdict.stuck_reason == "abandoned"
        assert verdict.recommended_action == "escalate"

    def test_round_trip(self) -> None:
        verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="solo_repo_no_reviewer",
            recommended_action="self_approve_on_solo",
            explanation="Solo maintainer, ready PR",
            confidence=0.9,
        )
        dumped = verdict.model_dump_json()
        restored = StuckVerdict.model_validate_json(dumped)
        assert restored == verdict

    def test_not_stuck_verdict(self) -> None:
        verdict = StuckVerdict(
            is_stuck=False,
            stuck_reason="not_stuck",
            recommended_action="wait",
            explanation="Fresh PR — just wait",
            confidence=0.95,
        )
        assert verdict.is_stuck is False

    def test_confidence_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            StuckVerdict(
                is_stuck=True,
                stuck_reason="abandoned",
                recommended_action="escalate",
                explanation="x",
                confidence=1.5,
            )

    def test_confidence_negative(self) -> None:
        with pytest.raises(ValidationError):
            StuckVerdict(
                is_stuck=True,
                stuck_reason="abandoned",
                recommended_action="escalate",
                explanation="x",
                confidence=-0.1,
            )

    def test_stuck_reason_closed_enum(self) -> None:
        with pytest.raises(ValidationError):
            StuckVerdict(
                is_stuck=True,
                stuck_reason="bogus",  # type: ignore[arg-type]
                recommended_action="escalate",
                explanation="x",
                confidence=0.5,
            )

    def test_recommended_action_closed_enum(self) -> None:
        with pytest.raises(ValidationError):
            StuckVerdict(
                is_stuck=True,
                stuck_reason="abandoned",
                recommended_action="nuke_from_orbit",  # type: ignore[arg-type]
                explanation="x",
                confidence=0.5,
            )

    def test_explanation_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            StuckVerdict(
                is_stuck=True,
                stuck_reason="abandoned",
                recommended_action="escalate",
                explanation="x" * 301,
                confidence=0.5,
            )


# ── Part 2: Legacy adapter ──────────────────────────────────────────────


class TestStuckFromLegacy:
    def test_stuck_true_maps_to_abandoned_escalate(self) -> None:
        verdict = stuck_from_legacy(True)
        assert verdict.is_stuck is True
        assert verdict.stuck_reason == "abandoned"
        assert verdict.recommended_action == "escalate"
        assert "Legacy heuristic" in verdict.explanation

    def test_stuck_false_maps_to_not_stuck_wait(self) -> None:
        verdict = stuck_from_legacy(False)
        assert verdict.is_stuck is False
        assert verdict.stuck_reason == "not_stuck"
        assert verdict.recommended_action == "wait"

    def test_legacy_confidence_is_bounded(self) -> None:
        # Legacy confidence is deliberately low — we have no evidence
        # the age cutoff was correct, only that it triggered.
        assert 0.0 <= stuck_from_legacy(True).confidence <= 1.0
        assert 0.0 <= stuck_from_legacy(False).confidence <= 1.0


# ── Part 3: LLM candidate prompt + happy path ───────────────────────────


class TestEvaluateStuckPRLLM:
    def test_prompt_contains_required_payload(self) -> None:
        pr = make_pr(
            number=42,
            labels=[Label(name="maintainer:upgrade"), Label(name="area:config")],
            created_at=datetime.now(UTC) - timedelta(hours=50),
        )
        pr = pr.model_copy(update={"title": "Migrate stuck-PR gate"})
        readiness_verdict = Readiness(
            verdict="needs_human",
            confidence=0.7,
            blockers=[],
            summary="Blocked by upstream dependency.",
        )
        ctx = PRStuckContext(
            pr=pr,
            age_hours=50.0,
            last_activity_hours=48.0,
            check_runs=[make_check_run(name="lint")],
            reviews=[make_review(body="LGTM")],
            readiness_verdict=readiness_verdict,
            linked_issues=["#37"],
            repo_slug="ianlintner/caretaker",
            collaborator_count=1,
        )
        prompt = build_stuck_pr_prompt(ctx)
        assert "Migrate stuck-PR gate" in prompt
        assert "#42" in prompt
        assert "ianlintner/caretaker" in prompt
        assert "maintainer:upgrade" in prompt
        assert "Age: 50.0h" in prompt
        assert "Collaborator count: 1" in prompt
        assert "needs_human" in prompt
        assert "lint" in prompt
        assert "LGTM" in prompt
        assert "#37" in prompt

    def test_prompt_handles_missing_optional_fields(self) -> None:
        pr = make_pr(number=1)
        ctx = PRStuckContext(pr=pr, age_hours=5.0)
        prompt = build_stuck_pr_prompt(ctx)
        assert "(no reviews yet)" in prompt
        assert "(no check runs)" in prompt
        assert "Last activity: unknown" in prompt
        assert "Collaborator count: unknown" in prompt
        assert "(none)" in prompt

    async def test_happy_path_returns_schema_instance(self) -> None:
        pr = make_pr(number=7)
        fake_verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="ci_deadlock",
            recommended_action="escalate",
            explanation="CI stuck in_progress for 48h",
            confidence=0.85,
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
        ctx = PRStuckContext(pr=pr, age_hours=48.0, repo_slug="ian/demo")
        result = await evaluate_stuck_pr_llm(ctx, claude=claude)  # type: ignore[arg-type]
        assert result is fake_verdict
        assert len(claude.calls) == 1
        call = claude.calls[0]
        assert call["feature"] == "pr_stuck"
        assert call["schema"] is StuckVerdict
        assert "ian/demo" in call["prompt"]
        assert "#7" in call["prompt"]
        assert call["system"] is not None
        assert "stuck-PR classifier" in call["system"]

    async def test_structured_complete_error_returns_none(self) -> None:
        pr = make_pr(number=9)

        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="not-json", validation_error=ValueError("bad")
        )

        ctx = PRStuckContext(pr=pr, age_hours=30.0, repo_slug="ian/demo")
        result = await evaluate_stuck_pr_llm(ctx, claude=claude)
        assert result is None


# ── Part 4: Agent-level pre-filter + shadow integration ─────────────────


async def _run_process_pr(
    pr: Any,
    tracking: TrackedPR,
    config: PRAgentConfig,
    *,
    reviews: list[Any] | None = None,
    check_runs: list[Any] | None = None,
    llm_router: Any = None,
) -> tuple[TrackedPR, PRAgentReport, PRAgent]:
    github = AsyncMock()
    github.get_check_runs = AsyncMock(return_value=check_runs or [])
    github.get_pr_reviews = AsyncMock(return_value=reviews or [])
    agent = PRAgent(
        github=github,
        owner="o",
        repo="r",
        config=config,
        llm_router=llm_router,
    )
    report = PRAgentReport()
    updated = await agent._process_pr(pr, tracking, report)
    return updated, report, agent


class TestPreFilter:
    """The minimum-age pre-filter: PRs younger than ``stuck_age_hours``
    must skip the shadow decision entirely — no legacy call, no LLM call,
    no ring-buffer record."""

    async def test_young_pr_skips_shadow_decision_entirely(self) -> None:
        _set_stuck_pr_mode("shadow")

        pr = make_pr(
            number=1,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=2),  # well under 24h
        )
        tracking = TrackedPR(number=1)
        config = _make_pr_config(stuck_age_hours=24)

        # Wire a claude that would count calls — none should happen.
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(
            return_value=StuckVerdict(
                is_stuck=True,
                stuck_reason="abandoned",
                recommended_action="escalate",
                explanation="x",
                confidence=0.5,
            )
        )
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        _, report, _ = await _run_process_pr(pr, tracking, config, llm_router=router)
        assert 1 not in report.escalated
        # Crucially, no shadow record at all.
        assert recent_records(name="stuck_pr") == []
        # And the candidate must never have been called.
        claude.structured_complete.assert_not_called()

    async def test_gate_disabled_when_stuck_age_hours_zero(self) -> None:
        _set_stuck_pr_mode("shadow")

        pr = make_pr(
            number=2,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        tracking = TrackedPR(number=2)
        config = _make_pr_config(stuck_age_hours=0)

        _, report, _ = await _run_process_pr(pr, tracking, config)
        assert 2 not in report.escalated
        assert recent_records(name="stuck_pr") == []


class TestShadowIntegration:
    """Three-mode shadow dispatch through ``PRAgent._process_pr``."""

    async def test_off_mode_uses_legacy_binary_verdict(self) -> None:
        _set_stuck_pr_mode("off")

        pr = make_pr(
            number=1,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=1)
        config = _make_pr_config(stuck_age_hours=24)

        updated, report, _ = await _run_process_pr(pr, tracking, config)
        # Legacy says stuck -> escalate.
        assert updated.state == PRTrackingState.ESCALATED
        assert 1 in report.escalated
        # Off mode must not persist any shadow record.
        assert recent_records(name="stuck_pr") == []

    async def test_shadow_mode_records_disagreement_returns_legacy(self) -> None:
        _set_stuck_pr_mode("shadow")

        # Legacy says stuck (48h old, no human review); LLM says merge_queue/wait.
        llm_verdict = StuckVerdict(
            is_stuck=False,
            stuck_reason="merge_queue",
            recommended_action="wait",
            explanation="Sitting in merge queue",
            confidence=0.9,
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=2,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=2)
        config = _make_pr_config(stuck_age_hours=24)

        updated, report, _ = await _run_process_pr(pr, tracking, config, llm_router=router)
        # Shadow mode returns legacy verdict -> escalation still fires.
        assert updated.state == PRTrackingState.ESCALATED
        assert 2 in report.escalated
        records = recent_records(name="stuck_pr")
        assert len(records) == 1
        rec = records[0]
        assert rec.outcome == "disagree"
        assert rec.mode == "shadow"
        assert rec.repo_slug == "o/r"
        assert rec.disagreement_reason is not None

    async def test_shadow_mode_agrees_when_actions_match(self) -> None:
        _set_stuck_pr_mode("shadow")

        # LLM agrees with legacy on is_stuck + recommended_action.
        llm_verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="abandoned",
            recommended_action="escalate",
            explanation="Different wording",
            confidence=0.9,
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=3,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=3)
        config = _make_pr_config(stuck_age_hours=24)

        await _run_process_pr(pr, tracking, config, llm_router=router)
        records = recent_records(name="stuck_pr")
        assert len(records) == 1
        assert records[0].outcome == "agree"
        assert records[0].disagreement_reason is None

    async def test_shadow_mode_swallows_candidate_runtime_error(self) -> None:
        _set_stuck_pr_mode("shadow")

        claude = MagicMock()
        claude.available = True
        # Raise a bare RuntimeError so the decorator records candidate_error.
        claude.structured_complete = AsyncMock(side_effect=RuntimeError("boom"))
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=4,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=4)
        config = _make_pr_config(stuck_age_hours=24)

        updated, report, _ = await _run_process_pr(pr, tracking, config, llm_router=router)
        # Legacy still fires -> PR escalated.
        assert updated.state == PRTrackingState.ESCALATED
        assert 4 in report.escalated
        records = recent_records(name="stuck_pr")
        assert len(records) == 1
        assert records[0].outcome == "candidate_error"

    async def test_enforce_mode_promotes_llm_verdict(self) -> None:
        _set_stuck_pr_mode("enforce")

        # Legacy would say stuck (48h, no human review); LLM says not-stuck/wait.
        llm_verdict = StuckVerdict(
            is_stuck=False,
            stuck_reason="merge_queue",
            recommended_action="wait",
            explanation="In a merge queue",
            confidence=0.92,
        )
        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(return_value=llm_verdict)
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=5,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=5)
        config = _make_pr_config(stuck_age_hours=24)

        updated, report, _ = await _run_process_pr(pr, tracking, config, llm_router=router)
        # Enforce mode: LLM says not stuck -> no escalation.
        assert 5 not in report.escalated
        assert updated.state != PRTrackingState.ESCALATED

    async def test_enforce_falls_through_to_legacy_without_llm(self) -> None:
        _set_stuck_pr_mode("enforce")

        pr = make_pr(
            number=6,
            user=User(login="copilot[bot]", id=1, type="Bot"),
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        tracking = TrackedPR(number=6)
        config = _make_pr_config(stuck_age_hours=24)

        # No llm_router — _candidate returns None; decorator falls
        # through to legacy, which says stuck -> escalate.
        updated, report, _ = await _run_process_pr(pr, tracking, config)
        assert updated.state == PRTrackingState.ESCALATED
        assert 6 in report.escalated


# ── Part 5: Solo-repo special case ──────────────────────────────────────


class TestSoloRepoNoReviewerPattern:
    """The P4 / M3 pattern from the fleet audit: a ready PR on a solo-
    maintainer repo that cannot clear the ``required_review_missing``
    blocker because there's nobody else to approve it."""

    async def test_solo_repo_ready_triggers_self_approve_on_solo(self) -> None:
        _set_stuck_pr_mode("enforce")

        # Candidate sees readiness.verdict=ready + collaborator_count=1
        # -> recommends self_approve_on_solo.
        expected_verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="solo_repo_no_reviewer",
            recommended_action="self_approve_on_solo",
            explanation="Solo maintainer repo with ready PR, nobody else can approve",
            confidence=0.93,
        )

        captured: dict[str, Any] = {}

        async def _fake_structured_complete(
            prompt: str,
            *,
            schema: type,
            feature: str,
            system: str | None = None,
            **kwargs: Any,
        ) -> Any:
            captured["prompt"] = prompt
            captured["feature"] = feature
            return expected_verdict

        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(side_effect=_fake_structured_complete)
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=99,
            user=User(login="ian", id=10, type="User"),
            created_at=datetime.now(UTC) - timedelta(hours=72),
        )
        tracking = TrackedPR(number=99)
        # Solo-repo: required_reviews=0 -> agent reports collaborator_count=1
        # into the prompt context.
        config = _make_pr_config(stuck_age_hours=24, required_reviews=0)

        # Preload a green CI check + an approved review so readiness says "ready".
        check_runs = [make_check_run(name="lint", conclusion=CheckConclusion.SUCCESS)]
        reviews = [
            make_review(
                user=User(login="ian", id=10, type="User"),
                state=ReviewState.APPROVED,
            )
        ]
        updated, report, _ = await _run_process_pr(
            pr,
            tracking,
            config,
            reviews=reviews,
            check_runs=check_runs,
            llm_router=router,
        )

        # In enforce mode the LLM verdict wins → is_stuck=True +
        # self_approve_on_solo → we escalate with the solo-repo reason.
        assert updated.state == PRTrackingState.ESCALATED
        assert 99 in report.escalated
        # Sanity-check the prompt saw the solo-repo signal.
        assert "Collaborator count: 1" in captured["prompt"]

    async def test_solo_repo_flag_flows_into_prompt(self) -> None:
        """When ``readiness.required_reviews == 0`` the agent passes
        ``collaborator_count=1`` to the stuck-PR context so the prompt
        can condition on the solo-repo signal."""
        _set_stuck_pr_mode("shadow")

        llm_verdict = StuckVerdict(
            is_stuck=True,
            stuck_reason="solo_repo_no_reviewer",
            recommended_action="self_approve_on_solo",
            explanation="solo repo",
            confidence=0.9,
        )
        captured: dict[str, Any] = {}

        async def _fake_structured_complete(
            prompt: str,
            *,
            schema: type,
            feature: str,
            system: str | None = None,
            **kwargs: Any,
        ) -> Any:
            captured["prompt"] = prompt
            return llm_verdict

        claude = MagicMock()
        claude.available = True
        claude.structured_complete = AsyncMock(side_effect=_fake_structured_complete)
        router = MagicMock()
        router.available = True
        router.claude_available = True
        router.claude = claude
        router.feature_enabled = MagicMock(return_value=False)

        pr = make_pr(
            number=100,
            user=User(login="ian", id=10, type="User"),
            created_at=datetime.now(UTC) - timedelta(hours=72),
        )
        tracking = TrackedPR(number=100)
        config = _make_pr_config(stuck_age_hours=24, required_reviews=0)

        await _run_process_pr(pr, tracking, config, llm_router=router)
        assert "Collaborator count: 1" in captured["prompt"]
