"""Tests for the PR complexity classifier.

Coverage:
- fast_path_tier: deterministic short-circuits before any LLM call
- classify: heuristic fallback when LLM is unavailable / errors
- _resolve_tier_model: tier → model resolution with fallback
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.evolution.executor_routing import ExecutorRouteContext, ExecutorRouteFile
from caretaker.pr_reviewer.backends.opencode_local import _resolve_tier_model
from caretaker.pr_reviewer.complexity_classifier import (
    ComplexityVerdict,
    classify,
    fast_path_tier,
)

# ── fast_path_tier ──────────────────────────────────────────────────────────


def test_fast_path_tiny_diff_is_trivial() -> None:
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="src/foo.py", additions=2, deletions=1)],
    )
    assert fast_path_tier(context=ctx) == "trivial"


def test_fast_path_workflow_change_is_complex_even_when_small() -> None:
    """One-line workflow tweak still needs a strong reviewer."""
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path=".github/workflows/ci.yml", additions=1, deletions=0)],
    )
    assert fast_path_tier(context=ctx) == "complex"


def test_fast_path_complex_label_overrides_size() -> None:
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="README.md", additions=1, deletions=0)],
        labels=["breaking-change"],
    )
    assert fast_path_tier(context=ctx) == "complex"


def test_fast_path_docs_label_with_small_diff_is_trivial() -> None:
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="docs/setup.md", additions=20, deletions=5)],
        labels=["docs"],
    )
    assert fast_path_tier(context=ctx) == "trivial"


def test_fast_path_medium_diff_defers_to_llm() -> None:
    """The classifier should defer mid-size, non-sensitive PRs to the LLM."""
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="src/foo.py", additions=80, deletions=20)],
    )
    assert fast_path_tier(context=ctx) is None


# ── classify (LLM path) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_uses_fast_path_when_possible() -> None:
    """Fast-path tiers don't burn an LLM call."""
    claude = MagicMock()
    claude.structured_complete = AsyncMock()
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="README.md", additions=1, deletions=1)],
        labels=["docs"],
    )
    verdict = await classify(context=ctx, claude=claude)
    assert verdict.tier == "trivial"
    claude.structured_complete.assert_not_called()


@pytest.mark.asyncio
async def test_classify_calls_llm_for_ambiguous_pr() -> None:
    claude = MagicMock()
    claude.available = True
    claude.structured_complete = AsyncMock(
        return_value=ComplexityVerdict(
            tier="standard", reason="ordinary feature work", confidence=0.85
        )
    )
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="src/foo.py", additions=80, deletions=20)],
    )
    verdict = await classify(context=ctx, claude=claude)
    assert verdict.tier == "standard"
    claude.structured_complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_falls_back_when_llm_unavailable() -> None:
    """No LLM → heuristic tier with conservative confidence."""
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="src/foo.py", additions=80, deletions=20)],
    )
    verdict = await classify(context=ctx, claude=None)
    assert verdict.tier == "standard"
    assert verdict.confidence == 0.6


@pytest.mark.asyncio
async def test_classify_falls_back_on_llm_error() -> None:
    """A structured_complete failure shouldn't break the caller."""
    from caretaker.llm.claude import StructuredCompleteError

    claude = MagicMock()
    claude.available = True
    claude.structured_complete = AsyncMock(
        side_effect=StructuredCompleteError(
            raw_text="bad json",
            validation_error=ValueError("schema mismatch"),
        )
    )
    ctx = ExecutorRouteContext(
        files=[ExecutorRouteFile(path="src/foo.py", additions=200, deletions=50)],
    )
    verdict = await classify(context=ctx, claude=claude)
    # Heuristic kicks in: 250 LOC → standard tier.
    assert verdict.tier == "standard"


# ── _resolve_tier_model ─────────────────────────────────────────────────────


def test_resolve_tier_model_uses_tier_map_entry() -> None:
    tier_map = {"trivial": "cheap-model", "standard": "expensive-model"}
    assert _resolve_tier_model("trivial", tier_map=tier_map, default="default") == "cheap-model"
    assert (
        _resolve_tier_model("standard", tier_map=tier_map, default="default") == "expensive-model"
    )


def test_resolve_tier_model_falls_back_when_tier_missing() -> None:
    tier_map = {"trivial": "cheap-model"}
    assert _resolve_tier_model("complex", tier_map=tier_map, default="default") == "default"


def test_resolve_tier_model_falls_back_when_tier_is_none() -> None:
    tier_map = {"trivial": "cheap-model"}
    assert _resolve_tier_model(None, tier_map=tier_map, default="default") == "default"


def test_resolve_tier_model_falls_back_when_entry_is_empty() -> None:
    tier_map = {"trivial": ""}
    assert _resolve_tier_model("trivial", tier_map=tier_map, default="default") == "default"
