"""Integration: evaluate_pr_readiness_llm uses the consensus engine when configured."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from caretaker.consensus import active as consensus_active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.pr_agent.readiness_llm import Readiness


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    available: bool = True

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
        if model is not None:
            queue = self.responses.get(model, [])
        else:
            queue = self.responses.get("<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _readiness(verdict: str, confidence: float = 0.9) -> Readiness:
    return Readiness(
        verdict=verdict,  # type: ignore[arg-type]
        summary=f"r summary {verdict}",
        confidence=confidence,
        blockers=[],
    )


@pytest.fixture(autouse=True)
def _reset_active() -> None:
    consensus_active.reset_for_tests()


@dataclass
class _FakePR:
    number: int = 1
    title: str = "Test PR"
    body: str | None = "Body"
    draft: bool = False
    mergeable: bool | None = True
    labels: list[Any] = field(default_factory=list)


def _build_ctx() -> Any:
    """Build a PRReadinessContext sufficient for build_readiness_prompt to render.

    Constructed via the real dataclass constructor (rather than ``__new__``)
    so future required fields surface as a constructor TypeError instead of
    silently leaving the test stub partially populated. ``pr`` is duck-typed
    — ``PullRequest`` is a TYPE_CHECKING-only annotation on the dataclass.
    """
    from caretaker.pr_agent.readiness_llm import PRReadinessContext

    return PRReadinessContext(
        pr=_FakePR(),  # type: ignore[arg-type]  # duck-typed; PullRequest is TYPE_CHECKING-only
        check_runs=[],
        reviews=[],
        linked_issues=[],
        repo_slug="owner/repo",
        is_solo_maintainer=True,
    )


@pytest.mark.asyncio
async def test_readiness_uses_engine_when_active() -> None:
    """When consensus engine is active, evaluate_pr_readiness_llm calls engine."""
    from caretaker.pr_agent.readiness_llm import evaluate_pr_readiness_llm

    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_readiness("ready", confidence=0.92)],
            "fake-alt": [_readiness("ready", confidence=0.88)],
        },
    )
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool(
                {"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}
            ),
            sites={
                "readiness": SiteConfig(
                    strategy="always_two_models",
                    primary="reasoning_anthropic",
                    escalation=["reasoning_alt"],
                    confidence_threshold=0.7,
                    agreement_fields=["verdict"],
                ),
            },
        ),
        claude=claude,
    )
    consensus_active.configure(engine)

    ctx = _build_ctx()

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    assert verdict is not None
    assert verdict.verdict == "ready"
    assert "fake-anthropic" in claude.calls
    assert "fake-alt" in claude.calls


@pytest.mark.asyncio
async def test_readiness_falls_back_when_engine_unavailable() -> None:
    """Engine raising ConsensusUnavailable returns None so shadow falls through."""
    from caretaker.llm.claude import StructuredCompleteError
    from caretaker.pr_agent.readiness_llm import evaluate_pr_readiness_llm

    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-anthropic": [err], "fake-alt": [err]})
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool(
                {"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}
            ),
            sites={
                "readiness": SiteConfig(
                    strategy="always_two_models",
                    primary="reasoning_anthropic",
                    escalation=["reasoning_alt"],
                    confidence_threshold=0.7,
                    agreement_fields=["verdict"],
                ),
            },
        ),
        claude=claude,
    )
    consensus_active.configure(engine)

    ctx = _build_ctx()

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    assert verdict is None  # ConsensusUnavailable -> None -> @shadow_decision falls through


@pytest.mark.asyncio
async def test_readiness_uses_direct_call_when_engine_inactive() -> None:
    """When no engine is active, falls back to the direct claude.structured_complete path."""
    from caretaker.pr_agent.readiness_llm import evaluate_pr_readiness_llm

    consensus_active.reset_for_tests()
    claude = _FakeClaude(responses={"<default>": [_readiness("blocked", confidence=0.6)]})

    ctx = _build_ctx()

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    assert verdict is not None
    assert verdict.verdict == "blocked"
    assert claude.calls == ["<default>"]  # Single direct call, model=None
