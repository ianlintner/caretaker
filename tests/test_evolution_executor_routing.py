"""Tests for the Phase 2 LLM-backed executor routing migration (T-A9).

Covers every call-out in the task brief:

* Schema validation (happy path + enum/length guards).
* Legacy adapter mapping for both call sites:
  * :func:`route_from_pr_reviewer_legacy` — inline vs claude_code.
  * :func:`route_from_foundry_legacy`     — foundry vs copilot.
* LLM candidate: prompt payload + return value on happy path +
  :class:`StructuredCompleteError` handling.
* Shadow decorator integration in all three modes (off/shadow/enforce).
* Risk-tag assertions for a PR touching ``.github/workflows/``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from caretaker.config import AgenticConfig, AgenticDomainConfig
from caretaker.evolution import shadow_config
from caretaker.evolution.executor_routing import (
    ExecutorRoute,
    ExecutorRouteContext,
    ExecutorRouteFile,
    build_routing_prompt,
    executor_routes_agree,
    route_executor_llm,
    route_from_foundry_legacy,
    route_from_pr_reviewer_legacy,
)
from caretaker.evolution.shadow import (
    clear_records_for_tests,
    recent_records,
    shadow_decision,
)
from caretaker.foundry.size_classifier import ClassifierResult, Decision
from caretaker.graph import writer as graph_writer
from caretaker.llm.claude import StructuredCompleteError
from caretaker.pr_reviewer.routing import RoutingDecision

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_shadow_state() -> None:
    clear_records_for_tests()
    shadow_config.reset_for_tests()
    graph_writer.reset_for_tests()


def _set_routing_mode(mode: str) -> None:
    cfg = AgenticConfig(executor_routing=AgenticDomainConfig(mode=mode))  # type: ignore[arg-type]
    shadow_config.configure(cfg)


# ── Schema validation ────────────────────────────────────────────────────


class TestExecutorRouteSchema:
    def test_happy_path_parses(self) -> None:
        route = ExecutorRoute(
            path="inline",
            reason="small docs-only PR",
            risk_tags=["safe"],
            confidence=0.8,
        )
        assert route.path == "inline"
        assert route.reason == "small docs-only PR"
        assert route.risk_tags == ["safe"]
        assert route.confidence == 0.8

    def test_invalid_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorRoute(path="invalid", reason="r", confidence=0.5)  # type: ignore[arg-type]

    def test_invalid_risk_tag_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorRoute(
                path="inline",
                reason="r",
                risk_tags=["bogus"],  # type: ignore[list-item]
                confidence=0.5,
            )

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorRoute(path="inline", reason="r", confidence=1.5)

    def test_reason_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            ExecutorRoute(path="inline", reason="x" * 301, confidence=0.5)

    def test_risk_tags_default_empty(self) -> None:
        route = ExecutorRoute(path="foundry", reason="x", confidence=0.7)
        assert route.risk_tags == []


# ── Legacy adapter: pr_reviewer ──────────────────────────────────────────


class TestRouteFromPRReviewerLegacy:
    def test_use_inline_maps_to_inline_path(self) -> None:
        decision = RoutingDecision(score=12, use_inline=True, reason="loc=50(+6)")
        route = route_from_pr_reviewer_legacy(decision)
        assert route.path == "inline"
        assert "Legacy routing" in route.reason
        assert "score=12" in route.reason

    def test_score_above_threshold_maps_to_claude_code(self) -> None:
        decision = RoutingDecision(score=58, use_inline=False, reason="loc=800(+30)")
        route = route_from_pr_reviewer_legacy(decision)
        assert route.path == "claude_code"

    def test_workflows_path_produces_sensitive_tags(self) -> None:
        decision = RoutingDecision(score=72, use_inline=False, reason="sensitive_files(+15)")
        route = route_from_pr_reviewer_legacy(
            decision,
            additions=30,
            deletions=5,
            file_count=1,
            file_paths=[".github/workflows/ci.yml"],
        )
        assert route.path == "claude_code"
        assert "workflows_touched" in route.risk_tags
        assert "security_review_needed" in route.risk_tags

    def test_large_cross_package_diff_tagged(self) -> None:
        decision = RoutingDecision(score=80, use_inline=False, reason="loc=900(+30)")
        route = route_from_pr_reviewer_legacy(
            decision,
            additions=700,
            deletions=300,
            file_count=30,
            file_paths=[
                "a/file.py",
                "b/file.py",
                "c/file.py",
                "d/file.py",
                "e/file.py",
            ],
        )
        assert "large_diff" in route.risk_tags
        assert "cross_package" in route.risk_tags

    def test_auth_path_tagged(self) -> None:
        decision = RoutingDecision(score=55, use_inline=False, reason="sensitive(+10)")
        route = route_from_pr_reviewer_legacy(
            decision,
            file_paths=["src/app/auth_token.py"],
        )
        assert "auth_touched" in route.risk_tags
        assert "security_review_needed" in route.risk_tags

    def test_safe_when_nothing_matches(self) -> None:
        decision = RoutingDecision(score=5, use_inline=True, reason="low-complexity")
        route = route_from_pr_reviewer_legacy(
            decision,
            additions=10,
            deletions=5,
            file_count=1,
            file_paths=["README.md"],
        )
        assert route.risk_tags == ["safe"]

    def test_reason_truncated_to_schema_limit(self) -> None:
        decision = RoutingDecision(
            score=12,
            use_inline=True,
            reason="x" * 500,
        )
        route = route_from_pr_reviewer_legacy(decision)
        assert len(route.reason) <= 300


# ── Legacy adapter: foundry ──────────────────────────────────────────────


class TestRouteFromFoundryLegacy:
    def test_route_foundry_maps_to_foundry_path(self) -> None:
        result = ClassifierResult(decision=Decision.ROUTE_FOUNDRY, reason="eligible")
        route = route_from_foundry_legacy(result)
        assert route.path == "foundry"
        assert "route_foundry" in route.reason
        assert "Legacy routing" in route.reason

    def test_escalate_copilot_maps_to_copilot_path(self) -> None:
        result = ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason="task_type 'ARCH_REFACTOR' not in allowlist",
        )
        route = route_from_foundry_legacy(result)
        assert route.path == "copilot"
        assert "escalate_copilot" in route.reason

    def test_abort_maps_to_copilot_with_abort_reason(self) -> None:
        result = ClassifierResult(decision=Decision.ABORT, reason="refused")
        route = route_from_foundry_legacy(result)
        assert route.path == "copilot"
        assert "abort" in route.reason

    def test_workflows_file_raises_risk_tag(self) -> None:
        result = ClassifierResult(decision=Decision.ESCALATE_COPILOT, reason="too big")
        route = route_from_foundry_legacy(
            result,
            file_paths=[".github/workflows/release.yml"],
            additions=40,
            deletions=5,
            file_count=1,
        )
        assert "workflows_touched" in route.risk_tags
        assert "security_review_needed" in route.risk_tags


# ── Prompt builder ───────────────────────────────────────────────────────


class TestBuildRoutingPrompt:
    def test_prompt_contains_required_payload(self) -> None:
        ctx = ExecutorRouteContext(
            task_type="pr_review",
            files=[
                ExecutorRouteFile(path="src/a.py", additions=10, deletions=2),
                ExecutorRouteFile(path=".github/workflows/ci.yml", additions=5, deletions=0),
            ],
            labels=["maintainer:upgrade"],
            repo_slug="ian/demo",
            candidate_paths=["inline", "claude_code"],
            title="Bump ruff",
            body="Why not.",
        )
        prompt = build_routing_prompt(ctx)
        assert "pr_review" in prompt
        assert "ian/demo" in prompt
        assert "Bump ruff" in prompt
        assert "maintainer:upgrade" in prompt
        assert "src/a.py" in prompt
        assert ".github/workflows/ci.yml" in prompt
        # Sensitive-path hint echoed from legacy regex table.
        assert "workflows" in prompt
        # Candidate paths enumerated.
        assert "inline" in prompt and "claude_code" in prompt
        # Totals computed.
        assert "Total additions: 15" in prompt
        assert "Total deletions: 2" in prompt

    def test_prompt_body_truncated(self) -> None:
        ctx = ExecutorRouteContext(body="x" * 2000)
        prompt = build_routing_prompt(ctx)
        # Body trimmed to 1000 chars plus marker.
        assert "..." in prompt
        assert prompt.count("x") <= 1000


# ── LLM candidate ────────────────────────────────────────────────────────


class TestRouteExecutorLLM:
    async def test_happy_path_returns_schema_instance(self) -> None:
        fake_route = ExecutorRoute(
            path="inline",
            reason="Small doc-only PR; no sensitive paths.",
            risk_tags=["safe"],
            confidence=0.92,
        )

        class _FakeClaude:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
                self.available = True

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
                return fake_route

        claude = _FakeClaude()
        ctx = ExecutorRouteContext(
            task_type="pr_review",
            repo_slug="ian/demo",
            candidate_paths=["inline", "claude_code"],
            title="docs: typo",
        )
        result = await route_executor_llm(ctx, claude=claude)  # type: ignore[arg-type]
        assert result is fake_route
        assert len(claude.calls) == 1
        call = claude.calls[0]
        assert call["feature"] == "executor_routing"
        assert call["schema"] is ExecutorRoute
        assert "ian/demo" in call["prompt"]
        assert call["system"] is not None
        assert "executor router" in call["system"]

    async def test_structured_complete_error_returns_none(self) -> None:
        claude = AsyncMock()
        claude.structured_complete.side_effect = StructuredCompleteError(
            raw_text="nope", validation_error=ValueError("bad")
        )
        ctx = ExecutorRouteContext(repo_slug="ian/demo")
        result = await route_executor_llm(ctx, claude=claude)
        assert result is None


# ── Risk-tag assertion for workflows-touched PR ──────────────────────────


class TestWorkflowsTouchedRiskTags:
    """Brief-mandated test: a PR that touches ``.github/workflows/`` must
    surface ``workflows_touched`` + ``security_review_needed`` in the
    legacy adapter's risk-tag output.
    """

    def test_pr_reviewer_legacy_adds_both_tags(self) -> None:
        decision = RoutingDecision(score=85, use_inline=False, reason="workflows touched")
        route = route_from_pr_reviewer_legacy(
            decision,
            additions=20,
            deletions=10,
            file_count=1,
            file_paths=[".github/workflows/deploy.yml"],
        )
        assert route.path == "claude_code"
        assert "workflows_touched" in route.risk_tags
        assert "security_review_needed" in route.risk_tags

    def test_foundry_legacy_adds_both_tags(self) -> None:
        result = ClassifierResult(decision=Decision.ESCALATE_COPILOT, reason="not a candidate")
        route = route_from_foundry_legacy(
            result,
            file_paths=[".github/workflows/release.yml"],
        )
        assert "workflows_touched" in route.risk_tags
        assert "security_review_needed" in route.risk_tags


# ── Compare helper ───────────────────────────────────────────────────────


class TestExecutorRoutesAgree:
    def test_same_path_agrees_regardless_of_reason(self) -> None:
        a = ExecutorRoute(path="inline", reason="legacy", confidence=0.9)
        b = ExecutorRoute(path="inline", reason="llm says so", confidence=0.7)
        assert executor_routes_agree(a, b) is True

    def test_different_path_disagrees(self) -> None:
        a = ExecutorRoute(path="inline", reason="legacy", confidence=0.9)
        b = ExecutorRoute(path="claude_code", reason="llm", confidence=0.7)
        assert executor_routes_agree(a, b) is False


# ── Shadow decorator integration ─────────────────────────────────────────


class TestShadowDecoratorIntegration:
    async def test_off_mode_skips_candidate(self) -> None:
        _set_routing_mode("off")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        legacy_called = 0
        candidate_called = 0

        async def legacy() -> ExecutorRoute:
            nonlocal legacy_called
            legacy_called += 1
            return ExecutorRoute(path="inline", reason="legacy", confidence=0.9)

        async def candidate() -> ExecutorRoute | None:
            nonlocal candidate_called
            candidate_called += 1
            return ExecutorRoute(path="claude_code", reason="candidate", confidence=0.7)

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.path == "inline"
        assert legacy_called == 1
        assert candidate_called == 0
        assert recent_records() == []

    async def test_shadow_mode_records_disagreement(self) -> None:
        _set_routing_mode("shadow")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> ExecutorRoute:
            return ExecutorRoute(path="inline", reason="legacy", confidence=0.5)

        async def candidate() -> ExecutorRoute | None:
            return ExecutorRoute(path="claude_code", reason="candidate", confidence=0.9)

        verdict = await decide(
            legacy=legacy,
            candidate=candidate,
            context={"repo_slug": "ian/demo", "pr_number": 4},
        )
        # Shadow mode always returns the legacy verdict so downstream
        # behavior stays byte-identical.
        assert verdict.path == "inline"
        records = recent_records()
        assert len(records) == 1
        assert records[0].outcome == "disagree"
        assert records[0].name == "executor_routing"
        assert records[0].repo_slug == "ian/demo"

    async def test_shadow_mode_agrees_when_paths_match(self) -> None:
        _set_routing_mode("shadow")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> ExecutorRoute:
            # Differ on reason/confidence/risk_tags but agree on path.
            return ExecutorRoute(
                path="foundry",
                reason="Legacy routing: score=N, ...",
                risk_tags=["safe"],
                confidence=0.9,
            )

        async def candidate() -> ExecutorRoute | None:
            return ExecutorRoute(
                path="foundry",
                reason="LLM: small well-scoped change",
                risk_tags=[],
                confidence=0.7,
            )

        verdict = await decide(
            legacy=legacy,
            candidate=candidate,
            context={"repo_slug": "ian/demo"},
        )
        assert verdict.path == "foundry"
        records = recent_records()
        # Agreement — no disagreement record written (but an ``agree``
        # record is persisted for audit).
        assert len(records) == 1
        assert records[0].outcome == "agree"

    async def test_enforce_mode_promotes_candidate(self) -> None:
        _set_routing_mode("enforce")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> ExecutorRoute:
            return ExecutorRoute(path="inline", reason="legacy", confidence=0.5)

        async def candidate() -> ExecutorRoute | None:
            return ExecutorRoute(path="claude_code", reason="llm", confidence=0.95)

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.path == "claude_code"
        # enforced_candidate outcome is counted but not persisted as a
        # disagreement record.
        assert recent_records() == []

    async def test_enforce_mode_falls_through_on_none(self) -> None:
        _set_routing_mode("enforce")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        async def legacy() -> ExecutorRoute:
            return ExecutorRoute(path="inline", reason="legacy ok", confidence=1.0)

        async def candidate() -> ExecutorRoute | None:
            return None  # simulates StructuredCompleteError inside candidate

        verdict = await decide(legacy=legacy, candidate=candidate)
        assert verdict.path == "inline"

    async def test_shared_name_aggregates_across_sites(self) -> None:
        """Both call sites register under the same ``executor_routing``
        name, so a single config switch controls both and the ring buffer
        / Prometheus counter report aggregate disagreement rates across
        the full executor fleet.
        """
        _set_routing_mode("shadow")

        @shadow_decision("executor_routing", compare=executor_routes_agree)
        async def decide(*, legacy: Any, candidate: Any, context: Any = None) -> ExecutorRoute:
            raise AssertionError("wrapper short-circuits")

        async def legacy_pr_reviewer() -> ExecutorRoute:
            return ExecutorRoute(path="inline", reason="pr-reviewer legacy", confidence=0.9)

        async def candidate_pr_reviewer() -> ExecutorRoute | None:
            return ExecutorRoute(path="claude_code", reason="pr-reviewer llm", confidence=0.8)

        async def legacy_foundry() -> ExecutorRoute:
            return ExecutorRoute(path="foundry", reason="foundry legacy", confidence=0.9)

        async def candidate_foundry() -> ExecutorRoute | None:
            return ExecutorRoute(path="copilot", reason="foundry llm", confidence=0.8)

        await decide(
            legacy=legacy_pr_reviewer,
            candidate=candidate_pr_reviewer,
            context={"site": "pr_reviewer"},
        )
        await decide(
            legacy=legacy_foundry,
            candidate=candidate_foundry,
            context={"site": "foundry"},
        )
        records = recent_records()
        # Two disagreement records, both under the shared decision name.
        assert len(records) == 2
        assert {r.name for r in records} == {"executor_routing"}
        assert all(r.outcome == "disagree" for r in records)
