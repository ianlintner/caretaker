"""Tests for the size_classifier hybrid floor/ceiling + LLM borderline path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from caretaker.consensus import active as consensus_active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.foundry.size_classifier import (
    Decision,
    SizeVerdict,
    decide_post,
    decide_pre,
)


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def structured_complete(
        self,
        prompt: str,
        *,
        schema: type[Any],
        feature: str,
        system: str | None = None,
        model: str | None = None,
        max_retries: int | None = None,
        max_tokens: int = 2000,
    ) -> Any:
        self.calls.append(model or "<default>")
        queue = self.responses.get(model or "<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _reset_active() -> None:
    consensus_active.reset_for_tests()


# ── Floor: counts well under low band always route to Foundry ──────────────


@pytest.mark.asyncio
async def test_decide_post_floor_routes_to_foundry_without_engine() -> None:
    result = await decide_post(
        files_changed=1,
        insertions=10,
        deletions=2,
        max_files_touched=20,
        max_diff_lines=400,
        borderline_low_files=5,
        borderline_high_files=15,
        borderline_low_lines=100,
        borderline_high_lines=300,
    )
    assert result.decision == Decision.ROUTE_FOUNDRY


# ── Ceiling: counts well over high band always escalate ────────────────────


@pytest.mark.asyncio
async def test_decide_post_ceiling_escalates_without_engine() -> None:
    result = await decide_post(
        files_changed=25,
        insertions=600,
        deletions=200,
        max_files_touched=20,
        max_diff_lines=400,
        borderline_low_files=5,
        borderline_high_files=15,
        borderline_low_lines=100,
        borderline_high_lines=300,
    )
    assert result.decision == Decision.ESCALATE_COPILOT


# ── Borderline: in the gray zone, consult the engine ───────────────────────


@pytest.mark.asyncio
async def test_decide_post_borderline_consults_engine() -> None:
    claude = _FakeClaude(
        responses={
            "fake-fast": [
                SizeVerdict(
                    decision=Decision.ROUTE_FOUNDRY, confidence=0.85, reason="mechanical refactor"
                )
            ],
        },
    )
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
            sites={
                "size_classifier": SiteConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                    confidence_threshold=0.7,
                    agreement_fields=[],
                ),
            },
        ),
        claude=claude,
    )
    consensus_active.configure(engine)

    result = await decide_post(
        files_changed=10,  # in [5, 15] → borderline
        insertions=200,
        deletions=20,
        max_files_touched=20,
        max_diff_lines=400,
        borderline_low_files=5,
        borderline_high_files=15,
        borderline_low_lines=100,
        borderline_high_lines=300,
    )
    assert result.decision == Decision.ROUTE_FOUNDRY
    assert claude.calls == ["fake-fast"]


# ── Borderline + engine failure: fall back to count gate ───────────────────


@pytest.mark.asyncio
async def test_decide_post_borderline_falls_back_on_engine_failure() -> None:
    from caretaker.llm.claude import StructuredCompleteError

    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
            sites={
                "size_classifier": SiteConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                    confidence_threshold=0.7,
                    agreement_fields=[],
                ),
            },
        ),
        claude=claude,
    )
    consensus_active.configure(engine)

    # Borderline counts that the legacy gate would route to Foundry
    # (under both max thresholds).
    result = await decide_post(
        files_changed=10,
        insertions=200,
        deletions=20,
        max_files_touched=20,
        max_diff_lines=400,
        borderline_low_files=5,
        borderline_high_files=15,
        borderline_low_lines=100,
        borderline_high_lines=300,
    )
    assert result.decision == Decision.ROUTE_FOUNDRY


# ── decide_pre: only the error_output dimension has a borderline ──────────


@pytest.mark.asyncio
async def test_decide_pre_floor_passes_through() -> None:
    result = await decide_pre(
        task_type="lint_failure",
        allowed_task_types=["lint_failure"],
        head_repo_full_name="owner/repo",
        base_repo_full_name="owner/repo",
        route_same_repo_only=True,
        error_output="short failure",
        max_error_output_chars=16_000,
    )
    assert result.decision == Decision.ROUTE_FOUNDRY


@pytest.mark.asyncio
async def test_decide_pre_ceiling_escalates() -> None:
    result = await decide_pre(
        task_type="lint_failure",
        allowed_task_types=["lint_failure"],
        head_repo_full_name="owner/repo",
        base_repo_full_name="owner/repo",
        route_same_repo_only=True,
        error_output="x" * 20_000,
        max_error_output_chars=16_000,
    )
    assert result.decision == Decision.ESCALATE_COPILOT
