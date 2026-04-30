# LLM Consensus Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tiered-consensus engine that lets caretaker decision sites consult one or more models from a capability-tagged provider pool, and ship the LLM verdict as authoritative on `readiness` (always-two-models, cross-provider) and on `foundry/size_classifier` (tiered-confidence, only on borderline cases).

**Architecture:** New `caretaker/consensus/` module exposing a `ConsensusEngine` instantiated once at orchestrator startup and discovered via a process-wide holder (mirroring `evolution/shadow_config`). Strategies are pluggable; two ship: `tiered_confidence` and `always_two_models`. Provider tags resolve to concrete model strings through `LLMConfig.model_pool`. The engine reuses `ClaudeClient.structured_complete` for every model call. The existing `@shadow_decision` wrapper stays in place as a fallback-orchestrating safety net — when the engine raises `ConsensusUnavailable`, the legacy heuristic ships.

**Tech Stack:** Python 3.12+, pydantic v2, asyncio, prometheus_client, pytest, pytest-asyncio, ruff, mypy.

**Spec:** `docs/plans/2026-04-30-llm-consensus-engine-design.md`. Read that document before starting Task 1.

---

## File Structure

**New files:**

| Path | Responsibility |
|------|---------------|
| `src/caretaker/consensus/__init__.py` | Public surface — re-exports `ConsensusEngine`, `ConsensusResult`, `ConsensusTrace`, `ConsensusUnavailable`. |
| `src/caretaker/consensus/trace.py` | `ConsensusTrace`, `ModelAttempt` pydantic audit records. |
| `src/caretaker/consensus/result.py` | `ConsensusResult` dataclass, `ConsensusUnavailable` exception. |
| `src/caretaker/consensus/provider_pool.py` | `ProviderPool` — capability-tag → concrete model resolver. |
| `src/caretaker/consensus/strategies.py` | `Strategy` protocol; `TieredConfidence`, `AlwaysTwoModels` implementations. |
| `src/caretaker/consensus/engine.py` | `ConsensusEngine.decide(...)` — the public API. |
| `src/caretaker/consensus/active.py` | Process-wide engine holder (mirrors `evolution/shadow_config`). |
| `src/caretaker/consensus/metrics.py` | Prometheus counters/histograms specific to consensus. |
| `tests/test_consensus_trace.py` | `ConsensusTrace` / `ModelAttempt` roundtrip + helper tests. |
| `tests/test_consensus_provider_pool.py` | `ProviderPool` tag resolution / literal pass-through / errors. |
| `tests/test_consensus_strategies.py` | `TieredConfidence` + `AlwaysTwoModels` behaviour matrices. |
| `tests/test_consensus_engine.py` | `ConsensusEngine.decide` orchestration end-to-end with fakes. |
| `tests/test_consensus_active.py` | Process-wide holder tests. |
| `tests/test_pr_readiness_consensus.py` | Integration: `evaluate_pr_readiness_llm` uses the engine when configured; falls back when engine unavailable. |
| `tests/test_size_classifier_consensus.py` | Integration: hybrid floor/ceiling + borderline LLM consult. |

**Modified files:**

| Path | Why |
|------|-----|
| `src/caretaker/config.py` | `ModelPoolConfig`, `ConsensusDomainConfig`, `AgenticDomainConfig.consensus`, `AgenticConfig.size_classifier`. |
| `src/caretaker/evolution/shadow.py` | `ShadowDecisionRecord.consensus_trace_json: str | None`. |
| `src/caretaker/pr_agent/readiness_llm.py` | `evaluate_pr_readiness_llm` calls engine when active; falls back to direct `structured_complete` when not. |
| `src/caretaker/foundry/size_classifier.py` | New `decide_pre` / `decide_post` async wrappers with borderline-zone engine consult. |
| `src/caretaker/foundry/executor.py` | Calls `await decide_pre(...)` / `await decide_post(...)` instead of sync `pre_flight` / `post_flight`. |
| `src/caretaker/orchestrator.py` | Build engine at startup; configure `consensus.active` + `shadow_config.configure_maintainer`. |
| `caretaker_config.yaml.example` | Defaults documented for new pool + per-site consensus blocks. |
| `src/caretaker/doctor.py` | Validate consensus config at startup. |

---

## Task 1: ConsensusTrace + ModelAttempt audit records

**Files:**
- Create: `src/caretaker/consensus/__init__.py`
- Create: `src/caretaker/consensus/trace.py`
- Test: `tests/test_consensus_trace.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_trace.py`:

```python
"""Tests for ConsensusTrace and ModelAttempt audit records."""

from __future__ import annotations

from caretaker.consensus.trace import ConsensusTrace, ModelAttempt


def test_model_attempt_roundtrip() -> None:
    attempt = ModelAttempt(
        model="claude-sonnet-4-6",
        tag="reasoning_anthropic",
        latency_ms=412,
        confidence=0.82,
        verdict_summary="ready",
        error=None,
    )
    payload = attempt.model_dump_json()
    decoded = ModelAttempt.model_validate_json(payload)
    assert decoded == attempt


def test_consensus_trace_records_strategy_and_attempts() -> None:
    trace = ConsensusTrace(
        strategy="tiered_confidence",
        attempts=[
            ModelAttempt(
                model="haiku-4-5",
                tag="fast",
                latency_ms=120,
                confidence=0.55,
                verdict_summary="not_ready",
            ),
            ModelAttempt(
                model="claude-sonnet-4-6",
                tag="reasoning_anthropic",
                latency_ms=412,
                confidence=0.91,
                verdict_summary="ready",
            ),
        ],
        escalated=True,
        final_model="claude-sonnet-4-6",
    )
    assert trace.escalated is True
    assert len(trace.attempts) == 2
    assert trace.final_model == "claude-sonnet-4-6"

    payload = trace.model_dump_json()
    decoded = ConsensusTrace.model_validate_json(payload)
    assert decoded == trace


def test_model_attempt_with_error_records_failure() -> None:
    attempt = ModelAttempt(
        model="claude-sonnet-4-6",
        tag="reasoning_anthropic",
        latency_ms=4200,
        confidence=None,
        verdict_summary=None,
        error="StructuredCompleteError: timeout",
    )
    assert attempt.error == "StructuredCompleteError: timeout"
    assert attempt.confidence is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_trace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'caretaker.consensus'`.

- [ ] **Step 3: Create the module**

Create `src/caretaker/consensus/__init__.py`:

```python
"""Caretaker LLM consensus engine.

See ``docs/plans/2026-04-30-llm-consensus-engine-design.md`` for the design.
"""

from caretaker.consensus.trace import ConsensusTrace, ModelAttempt

__all__ = [
    "ConsensusTrace",
    "ModelAttempt",
]
```

Create `src/caretaker/consensus/trace.py`:

```python
"""Per-decision consensus audit records.

A :class:`ConsensusTrace` is the authoritative serialised audit for one
``engine.decide(...)`` call. It records every model attempt — including
errors — in attempt order, plus the final model whose verdict was shipped.
The record is JSON-serialised onto :attr:`ShadowDecisionRecord.consensus_trace_json`
for persistence and surfacing through the existing admin API.

Both types are :class:`pydantic.BaseModel` for cheap roundtrip; values
are scalar so the persisted JSON is grep-friendly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAttempt(BaseModel):
    """One model invocation inside a consensus decision."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Concrete model string (e.g. 'claude-sonnet-4-6').")
    tag: str = Field(
        description=(
            "Capability tag the model was resolved from "
            "('fast', 'reasoning_anthropic', etc.). Equals the literal model "
            "string when the caller passed a literal instead of a tag."
        ),
    )
    latency_ms: int = Field(ge=0, description="Wall-clock duration of the model call.")
    confidence: float | None = Field(
        default=None,
        description=(
            "Self-reported confidence from the model's verdict. ``None`` "
            "when the call errored or the verdict had no confidence field."
        ),
    )
    verdict_summary: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Short summary of the model's verdict for log readability. "
            "``None`` on error."
        ),
    )
    error: str | None = Field(
        default=None,
        description="Stringified exception when the call failed; otherwise ``None``.",
    )


class ConsensusTrace(BaseModel):
    """Audit record for one consensus decision."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(description="Strategy name: 'tiered_confidence' or 'always_two_models'.")
    attempts: list[ModelAttempt] = Field(
        default_factory=list,
        description="Model invocations in the order they happened.",
    )
    escalated: bool = Field(
        default=False,
        description=(
            "True when the strategy had to escalate beyond the primary model "
            "(low confidence in tiered, disagreement in always-two)."
        ),
    )
    final_model: str = Field(
        default="",
        description=(
            "The model whose verdict was returned to the caller. Empty string "
            "when the engine raised ConsensusUnavailable."
        ),
    )


__all__ = ["ConsensusTrace", "ModelAttempt"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_trace.py -v`
Expected: PASS — all three tests green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/__init__.py src/caretaker/consensus/trace.py tests/test_consensus_trace.py
git commit -m "$(cat <<'EOF'
feat(consensus): ConsensusTrace + ModelAttempt audit records

First building block of the consensus engine — the per-decision audit
record type. Serialised onto ShadowDecisionRecord.consensus_trace_json
in a later task.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: ConsensusResult + ConsensusUnavailable

**Files:**
- Create: `src/caretaker/consensus/result.py`
- Modify: `src/caretaker/consensus/__init__.py`
- Test: `tests/test_consensus_result.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_result.py`:

```python
"""Tests for ConsensusResult and ConsensusUnavailable."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt


class _Verdict(BaseModel):
    label: str
    confidence: float


def test_consensus_result_carries_verdict_and_trace() -> None:
    verdict = _Verdict(label="ready", confidence=0.91)
    trace = ConsensusTrace(
        strategy="tiered_confidence",
        attempts=[
            ModelAttempt(
                model="claude-sonnet-4-6",
                tag="reasoning_anthropic",
                latency_ms=412,
                confidence=0.91,
                verdict_summary="ready",
            ),
        ],
        final_model="claude-sonnet-4-6",
    )
    result = ConsensusResult(verdict=verdict, trace=trace)
    assert result.verdict is verdict
    assert result.trace is trace


def test_consensus_unavailable_carries_attempts() -> None:
    attempts = [
        ModelAttempt(
            model="haiku-4-5",
            tag="fast",
            latency_ms=2000,
            error="timeout",
        ),
    ]
    exc = ConsensusUnavailable(
        strategy="tiered_confidence",
        attempts=attempts,
        reason="all tiers exhausted",
    )
    assert exc.strategy == "tiered_confidence"
    assert exc.attempts == attempts
    assert exc.reason == "all tiers exhausted"
    assert "tiered_confidence" in str(exc)


def test_consensus_unavailable_is_exception() -> None:
    with pytest.raises(ConsensusUnavailable):
        raise ConsensusUnavailable(
            strategy="tiered_confidence",
            attempts=[],
            reason="nothing tried",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_result.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'caretaker.consensus.result'`.

- [ ] **Step 3: Create the result module**

Create `src/caretaker/consensus/result.py`:

```python
"""ConsensusResult and ConsensusUnavailable — the engine's return contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from caretaker.consensus.trace import ConsensusTrace, ModelAttempt

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ConsensusResult(Generic[T]):
    """Successful engine output.

    ``verdict`` is the verdict the strategy chose to ship. ``trace`` is the
    full per-model audit record — always populated, even on the happy path
    where no escalation happened.
    """

    verdict: T
    trace: ConsensusTrace


class ConsensusUnavailable(RuntimeError):
    """Raised when every model attempt failed.

    Carries the per-model :class:`ModelAttempt` records so the caller (and
    the wrapping ``@shadow_decision``) can persist a useful failure trail
    instead of just the exception message.
    """

    def __init__(
        self,
        *,
        strategy: str,
        attempts: list[ModelAttempt],
        reason: str,
    ) -> None:
        self.strategy = strategy
        self.attempts = attempts
        self.reason = reason
        super().__init__(f"consensus unavailable [{strategy}]: {reason}")


__all__ = ["ConsensusResult", "ConsensusUnavailable"]
```

Modify `src/caretaker/consensus/__init__.py`:

```python
"""Caretaker LLM consensus engine.

See ``docs/plans/2026-04-30-llm-consensus-engine-design.md`` for the design.
"""

from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt

__all__ = [
    "ConsensusResult",
    "ConsensusTrace",
    "ConsensusUnavailable",
    "ModelAttempt",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_result.py -v`
Expected: PASS — all three tests green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/result.py src/caretaker/consensus/__init__.py tests/test_consensus_result.py
git commit -m "$(cat <<'EOF'
feat(consensus): ConsensusResult + ConsensusUnavailable

Engine return contract. ConsensusResult carries a verdict + audit trace;
ConsensusUnavailable is the typed exception raised when every model
attempt fails.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: ProviderPool — capability-tag resolver

**Files:**
- Create: `src/caretaker/consensus/provider_pool.py`
- Test: `tests/test_consensus_provider_pool.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_provider_pool.py`:

```python
"""Tests for ProviderPool."""

from __future__ import annotations

import pytest

from caretaker.consensus.provider_pool import ProviderPool, ProviderPoolError


def test_resolves_known_tag_to_concrete_model() -> None:
    pool = ProviderPool({"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    assert pool.resolve("fast") == ("haiku-4-5", "fast")
    assert pool.resolve("reasoning_anthropic") == ("claude-sonnet-4-6", "reasoning_anthropic")


def test_literal_model_string_passes_through() -> None:
    """When the caller provides a literal model string, it's returned unchanged.

    Heuristic for "literal vs tag": literal contains a slash, dot, or hyphen
    that's not in any pool key. We treat any value that's not a pool key as
    a literal and pass it through to the LLM router.
    """
    pool = ProviderPool({"fast": "haiku-4-5"})
    assert pool.resolve("openai/gpt-4o") == ("openai/gpt-4o", "openai/gpt-4o")
    assert pool.resolve("claude-sonnet-4-6") == ("claude-sonnet-4-6", "claude-sonnet-4-6")


def test_unknown_empty_value_raises() -> None:
    pool = ProviderPool({"fast": "haiku-4-5"})
    with pytest.raises(ProviderPoolError):
        pool.resolve("")


def test_resolve_distinct_returns_two_distinct_models() -> None:
    pool = ProviderPool({"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    primary_model, _ = pool.resolve("fast")
    second_model, _ = pool.resolve_distinct("reasoning_anthropic", different_from=primary_model)
    assert second_model == "claude-sonnet-4-6"
    assert second_model != primary_model


def test_resolve_distinct_raises_when_same_concrete_model() -> None:
    """resolve_distinct raises when two tags resolve to the same model.

    This is the validation hook for AlwaysTwoModels — if 'reasoning_anthropic'
    and 'reasoning_alt' both happen to point at the same concrete model,
    the strategy must fail-fast at config time, not silently consult the
    same model twice.
    """
    pool = ProviderPool({"r1": "claude-sonnet-4-6", "r2": "claude-sonnet-4-6"})
    with pytest.raises(ProviderPoolError):
        pool.resolve_distinct("r2", different_from="claude-sonnet-4-6")


def test_pool_construction_rejects_empty_value() -> None:
    with pytest.raises(ProviderPoolError):
        ProviderPool({"fast": ""})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_provider_pool.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the provider pool**

Create `src/caretaker/consensus/provider_pool.py`:

```python
"""Capability-tag → concrete model resolver.

Pool entries are operator-defined: tags like ``fast``, ``reasoning_anthropic``,
``reasoning_alt``, ``cheap`` map to concrete model strings the LLM router
understands. Per-site consensus config (``ConsensusDomainConfig.primary`` /
``escalation``) accepts either a tag or a literal model string. The pool
treats any value not present as a key as a literal and passes it through.
"""

from __future__ import annotations


class ProviderPoolError(ValueError):
    """Raised on invalid pool construction or impossible resolution."""


class ProviderPool:
    """Resolves capability tags to concrete model strings.

    The pool is a thin tag dictionary. Resolution is total: any value that
    isn't a known tag is returned unchanged on the assumption it's a literal
    model string — the LLM router validates literals at call time.
    """

    def __init__(self, pool: dict[str, str]) -> None:
        for tag, model in pool.items():
            if not isinstance(model, str) or not model:
                raise ProviderPoolError(
                    f"pool tag {tag!r} maps to invalid value {model!r}; "
                    "every tag must resolve to a non-empty model string"
                )
        # Defensive copy so downstream mutations don't bleed back.
        self._pool: dict[str, str] = dict(pool)

    def resolve(self, value: str) -> tuple[str, str]:
        """Return ``(concrete_model, tag_or_literal)``.

        ``tag_or_literal`` is the original input — used by ``ModelAttempt``
        to record what the operator typed, even when it was a literal.
        """
        if not value:
            raise ProviderPoolError("cannot resolve empty model reference")
        if value in self._pool:
            return self._pool[value], value
        # Treat as literal; LLM router validates at call time.
        return value, value

    def resolve_distinct(self, value: str, *, different_from: str) -> tuple[str, str]:
        """Resolve ``value``; raise when the result equals ``different_from``.

        Used by AlwaysTwoModels strategy to enforce that two voting models
        are concretely different. The caller has already resolved the first
        model (``different_from``); this guards the second slot.
        """
        model, tag = self.resolve(value)
        if model == different_from:
            raise ProviderPoolError(
                f"{value!r} resolves to {model!r} which equals different_from "
                f"({different_from!r}); two-model agreement requires distinct concrete models"
            )
        return model, tag


__all__ = ["ProviderPool", "ProviderPoolError"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_provider_pool.py -v`
Expected: PASS — six tests green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/provider_pool.py tests/test_consensus_provider_pool.py
git commit -m "$(cat <<'EOF'
feat(consensus): ProviderPool tag resolver

Capability-tag → concrete-model resolver. Tags pass through to literals
when not in the pool. ``resolve_distinct`` enforces concrete-model
distinctness for AlwaysTwoModels.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Strategy protocol + TieredConfidence

**Files:**
- Create: `src/caretaker/consensus/strategies.py`
- Test: `tests/test_consensus_strategies.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_strategies.py`:

```python
"""Tests for consensus strategies.

Uses a fake ClaudeClient that returns canned verdicts to exercise the
strategy logic without hitting real LLMs. Each fake call records
which model was requested so the test can assert call shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field

from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable
from caretaker.consensus.strategies import (
    StrategyContext,
    TieredConfidence,
)
from caretaker.llm.claude import StructuredCompleteError


class _Verdict(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class _FakeClaude:
    """Fake ClaudeClient used by strategy tests.

    ``responses`` maps concrete model string → list of (verdict | exception)
    consumed in call order. ``calls`` records the model passed on each call.
    """

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
        assert model is not None, "strategy must pass an explicit model"
        self.calls.append(model)
        queue = self.responses.get(model, [])
        if not queue:
            raise StructuredCompleteError(
                raw_text="", validation_error=RuntimeError(f"no canned response for {model}")
            )
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _ctx(claude: _FakeClaude, *, primary: str, escalation: list[str], threshold: float = 0.7) -> StrategyContext:
    return StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary=primary,
        escalation=escalation,
        confidence_threshold=threshold,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )


# ── TieredConfidence ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tiered_ships_primary_when_above_threshold() -> None:
    claude = _FakeClaude(
        responses={"fake-fast": [_Verdict(label="ready", confidence=0.9)]},
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is False
    assert result.trace.final_model == "fake-fast"
    assert claude.calls == ["fake-fast"]


@pytest.mark.asyncio
async def test_tiered_escalates_when_below_threshold() -> None:
    claude = _FakeClaude(
        responses={
            "fake-fast": [_Verdict(label="not_ready", confidence=0.4)],
            "fake-strong": [_Verdict(label="ready", confidence=0.95)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is True
    assert result.trace.final_model == "fake-strong"
    assert claude.calls == ["fake-fast", "fake-strong"]


@pytest.mark.asyncio
async def test_tiered_picks_highest_confidence_in_escalation_tier() -> None:
    claude = _FakeClaude(
        responses={
            "fake-fast": [_Verdict(label="not_ready", confidence=0.3)],
            "fake-strong": [_Verdict(label="ready", confidence=0.65)],
            "literal-strongest": [_Verdict(label="needs_human", confidence=0.92)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic", "literal-strongest"])
    result = await strategy.run(ctx)
    # Highest confidence among escalation tier wins.
    assert result.verdict.label == "needs_human"
    assert result.trace.final_model == "literal-strongest"


@pytest.mark.asyncio
async def test_tiered_raises_when_all_models_error() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    with pytest.raises(ConsensusUnavailable) as excinfo:
        await strategy.run(ctx)
    assert excinfo.value.strategy == "tiered_confidence"
    assert len(excinfo.value.attempts) == 2


@pytest.mark.asyncio
async def test_tiered_recovers_when_only_primary_errors() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-fast": [err],
            "fake-strong": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = TieredConfidence()
    ctx = _ctx(claude, primary="fast", escalation=["reasoning_anthropic"])
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is True
    assert claude.calls == ["fake-fast", "fake-strong"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_strategies.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the strategies module**

Create `src/caretaker/consensus/strategies.py`:

```python
"""Pluggable consensus strategies.

Each strategy implements the same async ``run(ctx) -> ConsensusResult``
contract, raising :class:`ConsensusUnavailable` when every model attempt
errored out. The engine selects a strategy by name from
``ConsensusDomainConfig.strategy``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseModel")


@dataclass
class StrategyContext:
    """Inputs shared across every strategy invocation.

    Built once per ``engine.decide(...)`` call. Strategies do not mutate it.
    """

    site_name: str
    schema: type[Any]
    system_prompt: str
    user_prompt: str
    feature: str
    primary: str
    escalation: Sequence[str]
    confidence_threshold: float
    agreement_fields: Sequence[str]
    pool: ProviderPool
    claude: "ClaudeClient"
    max_tokens: int = 2000


class Strategy(Protocol):
    """Strategy protocol — every concrete strategy implements ``run``."""

    name: str

    async def run(self, ctx: StrategyContext) -> ConsensusResult[Any]: ...


# ── Helpers ───────────────────────────────────────────────────────────────


def _verdict_summary(verdict: Any) -> str:
    """Short, JSON-safe summary of a verdict for the audit trail."""
    for field_name in ("verdict", "label", "decision", "category"):
        value = getattr(verdict, field_name, None)
        if isinstance(value, str):
            return value[:100]
    return type(verdict).__name__


def _verdict_confidence(verdict: Any) -> float | None:
    """Read ``confidence`` off a verdict if it has one."""
    confidence = getattr(verdict, "confidence", None)
    if isinstance(confidence, (int, float)):
        return float(confidence)
    return None


async def _call_one(
    ctx: StrategyContext,
    *,
    tag_or_literal: str,
) -> tuple[Any | None, ModelAttempt]:
    """Resolve one tag/literal, call the model, build a ModelAttempt.

    Returns ``(verdict_or_None, attempt)``. ``verdict_or_None`` is ``None``
    when the call errored — the attempt's ``error`` field carries the
    stringified exception.
    """
    model, tag = ctx.pool.resolve(tag_or_literal)
    started = time.monotonic()
    try:
        verdict = await ctx.claude.structured_complete(
            ctx.user_prompt,
            schema=ctx.schema,
            feature=ctx.feature,
            system=ctx.system_prompt,
            model=model,
            max_tokens=ctx.max_tokens,
        )
    except StructuredCompleteError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return None, ModelAttempt(
            model=model,
            tag=tag,
            latency_ms=latency_ms,
            confidence=None,
            verdict_summary=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    return verdict, ModelAttempt(
        model=model,
        tag=tag,
        latency_ms=latency_ms,
        confidence=_verdict_confidence(verdict),
        verdict_summary=_verdict_summary(verdict),
        error=None,
    )


# ── TieredConfidence ──────────────────────────────────────────────────────


@dataclass
class TieredConfidence:
    """Primary model first; escalate if its self-reported confidence is low.

    Order of operations:

    1. Call ``primary``. If the verdict's ``confidence`` is ≥ threshold,
       ship it.
    2. Else call every entry in ``escalation`` in order, collecting verdicts
       and errors. If any verdict comes back, ship the **highest-confidence**
       one (treating ``None`` confidence as 0.0 — degraded model).
    3. If every model errored, raise :class:`ConsensusUnavailable`.

    Primary error → escalation runs (the tier failure isn't fatal until both
    tiers exhaust). This matches the spec's "single model error" handling.
    """

    name: str = "tiered_confidence"

    async def run(self, ctx: StrategyContext) -> ConsensusResult[Any]:
        attempts: list[ModelAttempt] = []
        primary_verdict, primary_attempt = await _call_one(ctx, tag_or_literal=ctx.primary)
        attempts.append(primary_attempt)

        if (
            primary_verdict is not None
            and primary_attempt.confidence is not None
            and primary_attempt.confidence >= ctx.confidence_threshold
        ):
            return ConsensusResult(
                verdict=primary_verdict,
                trace=ConsensusTrace(
                    strategy=self.name,
                    attempts=attempts,
                    escalated=False,
                    final_model=primary_attempt.model,
                ),
            )

        # Escalate.
        escalation_verdicts: list[tuple[Any, ModelAttempt]] = []
        for entry in ctx.escalation:
            verdict, attempt = await _call_one(ctx, tag_or_literal=entry)
            attempts.append(attempt)
            if verdict is not None:
                escalation_verdicts.append((verdict, attempt))

        if not escalation_verdicts and primary_verdict is None:
            raise ConsensusUnavailable(
                strategy=self.name,
                attempts=attempts,
                reason="primary errored and every escalation tier errored",
            )

        # If escalation produced anything, take the highest-confidence.
        candidate_pool: list[tuple[Any, ModelAttempt]] = list(escalation_verdicts)
        if primary_verdict is not None:
            candidate_pool.append((primary_verdict, primary_attempt))

        def _conf(item: tuple[Any, ModelAttempt]) -> float:
            return item[1].confidence if item[1].confidence is not None else 0.0

        winner_verdict, winner_attempt = max(candidate_pool, key=_conf)
        return ConsensusResult(
            verdict=winner_verdict,
            trace=ConsensusTrace(
                strategy=self.name,
                attempts=attempts,
                escalated=True,
                final_model=winner_attempt.model,
            ),
        )


__all__ = [
    "Strategy",
    "StrategyContext",
    "TieredConfidence",
]
```

- [ ] **Step 4: Add pytest-asyncio fixture mode if missing**

Check `pyproject.toml` for `pytest-asyncio` config:

Run: `grep -A 2 'asyncio_mode' pyproject.toml || echo "NOT FOUND"`

If not found, the existing tests (e.g. `tests/test_pr_reviewer_handoff_consumer.py`) should already exercise async — confirm they pass first to verify the fixture mode is set.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_strategies.py -v`
Expected: PASS — five `TieredConfidence` tests green.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/consensus/strategies.py tests/test_consensus_strategies.py
git commit -m "$(cat <<'EOF'
feat(consensus): TieredConfidence strategy + Strategy protocol

Primary-first; escalate on low self-reported confidence. Highest-confidence
verdict in the escalation tier wins. Raises ConsensusUnavailable when every
model errored.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: AlwaysTwoModels strategy

**Files:**
- Modify: `src/caretaker/consensus/strategies.py`
- Test: `tests/test_consensus_strategies.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consensus_strategies.py`:

```python
# ── AlwaysTwoModels ───────────────────────────────────────────────────────

from caretaker.consensus.strategies import AlwaysTwoModels


def _atm_ctx(claude: _FakeClaude, *, primary: str, escalation: list[str], agreement: list[str]) -> StrategyContext:
    return StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary=primary,
        escalation=escalation,
        confidence_threshold=0.7,
        agreement_fields=agreement,
        pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
        claude=claude,
    )


@pytest.mark.asyncio
async def test_atm_ships_primary_when_models_agree() -> None:
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    assert result.verdict.label == "ready"
    assert result.trace.escalated is False
    assert result.trace.final_model == "fake-anthropic"
    # Both must have been called (in parallel).
    assert sorted(claude.calls) == ["fake-alt", "fake-anthropic"]


@pytest.mark.asyncio
async def test_atm_escalates_to_tiebreaker_on_disagreement() -> None:
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
            "literal-tiebreaker": [_Verdict(label="needs_human", confidence=0.7)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    assert result.verdict.label == "needs_human"
    assert result.trace.escalated is True
    assert result.trace.final_model == "literal-tiebreaker"


@pytest.mark.asyncio
async def test_atm_promotes_tiebreaker_when_one_model_errors() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [err],
            "literal-tiebreaker": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        agreement=["label"],
    )
    result = await strategy.run(ctx)
    # Two votes ('fake-anthropic' + 'literal-tiebreaker') agree on 'ready'.
    assert result.verdict.label == "ready"
    # Final model is the surviving primary (it was the agreement winner).
    assert result.trace.final_model in ("fake-anthropic", "literal-tiebreaker")


@pytest.mark.asyncio
async def test_atm_raises_when_both_initial_models_error() -> None:
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [err],
            "fake-alt": [err],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=["label"],
    )
    with pytest.raises(ConsensusUnavailable):
        await strategy.run(ctx)


@pytest.mark.asyncio
async def test_atm_compares_full_verdict_when_agreement_fields_empty() -> None:
    """With agreement_fields=[], the strategy compares full verdicts via __eq__."""
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.9)],
        },
    )
    strategy = AlwaysTwoModels()
    ctx = _atm_ctx(
        claude,
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement=[],
    )
    result = await strategy.run(ctx)
    assert result.trace.escalated is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_strategies.py -v`
Expected: FAIL — `ImportError: cannot import name 'AlwaysTwoModels'`.

- [ ] **Step 3: Add AlwaysTwoModels to strategies.py**

Append to `src/caretaker/consensus/strategies.py`:

```python
# ── AlwaysTwoModels ───────────────────────────────────────────────────────


def _agree(a: Any, b: Any, fields: Sequence[str]) -> bool:
    """Decide whether two verdicts agree on the configured fields."""
    if not fields:
        try:
            return bool(a == b)
        except Exception:  # noqa: BLE001 — exotic __eq__ that raises
            return False
    for field_name in fields:
        if getattr(a, field_name, object()) != getattr(b, field_name, object()):
            return False
    return True


@dataclass
class AlwaysTwoModels:
    """Two-model agreement strategy with tiebreaker escalation.

    Order of operations:

    1. Call ``primary`` and ``escalation[0]`` in parallel. The escalation
       tag must resolve to a different concrete model than ``primary``;
       :func:`ProviderPool.resolve_distinct` enforces this.
    2. If both succeed and agree on ``agreement_fields`` (or full verdict
       if the list is empty), ship primary's verdict.
    3. Else (disagreement, or one errored) call ``escalation[1:]`` in
       order until the next available model produces a verdict — that's
       the tiebreaker.
    4. Tiebreaker's verdict ships if it agrees with at least one of the
       prior verdicts on the agreement fields; otherwise the highest-
       confidence verdict among all attempts wins.
    5. If every model errored, raise :class:`ConsensusUnavailable`.
    """

    name: str = "always_two_models"

    async def run(self, ctx: StrategyContext) -> ConsensusResult[Any]:
        if not ctx.escalation:
            raise ConsensusUnavailable(
                strategy=self.name,
                attempts=[],
                reason="always_two_models requires escalation[0] (no second model configured)",
            )

        # Resolve primary first; resolve escalation[0] distinctly.
        primary_model, _ = ctx.pool.resolve(ctx.primary)
        # Validate distinctness (raises if same concrete model).
        ctx.pool.resolve_distinct(ctx.escalation[0], different_from=primary_model)

        # Run primary + secondary in parallel.
        first_call = _call_one(ctx, tag_or_literal=ctx.primary)
        second_call = _call_one(ctx, tag_or_literal=ctx.escalation[0])
        (primary_verdict, primary_attempt), (second_verdict, second_attempt) = await asyncio.gather(
            first_call, second_call
        )
        attempts: list[ModelAttempt] = [primary_attempt, second_attempt]

        # Both succeed and agree → ship primary.
        if (
            primary_verdict is not None
            and second_verdict is not None
            and _agree(primary_verdict, second_verdict, ctx.agreement_fields)
        ):
            return ConsensusResult(
                verdict=primary_verdict,
                trace=ConsensusTrace(
                    strategy=self.name,
                    attempts=attempts,
                    escalated=False,
                    final_model=primary_attempt.model,
                ),
            )

        # Disagreement OR a leg errored → run tiebreaker tier.
        tiebreaker_verdict: Any | None = None
        tiebreaker_attempt: ModelAttempt | None = None
        for entry in ctx.escalation[1:]:
            verdict, attempt = await _call_one(ctx, tag_or_literal=entry)
            attempts.append(attempt)
            if verdict is not None:
                tiebreaker_verdict = verdict
                tiebreaker_attempt = attempt
                break

        # Promote whatever's available to a winner.
        candidates: list[tuple[Any, ModelAttempt]] = []
        if primary_verdict is not None:
            candidates.append((primary_verdict, primary_attempt))
        if second_verdict is not None:
            candidates.append((second_verdict, second_attempt))
        if tiebreaker_verdict is not None and tiebreaker_attempt is not None:
            candidates.append((tiebreaker_verdict, tiebreaker_attempt))

        if not candidates:
            raise ConsensusUnavailable(
                strategy=self.name,
                attempts=attempts,
                reason="primary, secondary, and every tiebreaker errored",
            )

        # If tiebreaker exists and agrees with at least one prior, prefer it.
        if tiebreaker_verdict is not None and tiebreaker_attempt is not None:
            for verdict, attempt in candidates[:-1]:  # exclude tiebreaker self
                if _agree(tiebreaker_verdict, verdict, ctx.agreement_fields):
                    return ConsensusResult(
                        verdict=tiebreaker_verdict,
                        trace=ConsensusTrace(
                            strategy=self.name,
                            attempts=attempts,
                            escalated=True,
                            final_model=tiebreaker_attempt.model,
                        ),
                    )

        # Fall back to highest-confidence among all candidates.
        def _conf(item: tuple[Any, ModelAttempt]) -> float:
            return item[1].confidence if item[1].confidence is not None else 0.0

        winner_verdict, winner_attempt = max(candidates, key=_conf)
        return ConsensusResult(
            verdict=winner_verdict,
            trace=ConsensusTrace(
                strategy=self.name,
                attempts=attempts,
                escalated=True,
                final_model=winner_attempt.model,
            ),
        )
```

Update the `__all__` at the bottom of the file:

```python
__all__ = [
    "AlwaysTwoModels",
    "Strategy",
    "StrategyContext",
    "TieredConfidence",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_strategies.py -v`
Expected: PASS — all `TieredConfidence` and `AlwaysTwoModels` tests green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/strategies.py tests/test_consensus_strategies.py
git commit -m "$(cat <<'EOF'
feat(consensus): AlwaysTwoModels strategy

Primary + secondary in parallel; agreement on configured fields ships the
primary verdict. Disagreement or single-model error escalates to a
tiebreaker tier. Validates distinct concrete models for the two-model gate.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: ConsensusEngine

**Files:**
- Create: `src/caretaker/consensus/engine.py`
- Modify: `src/caretaker/consensus/__init__.py`
- Test: `tests/test_consensus_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_engine.py`:

```python
"""Tests for ConsensusEngine.decide orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable


class _Verdict(BaseModel):
    label: str
    confidence: float


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
        assert model is not None
        self.calls.append(model)
        queue = self.responses.get(model, [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _engine(claude: _FakeClaude, *, sites: dict[str, SiteConfig]) -> ConsensusEngine:
    return ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong", "reasoning_alt": "fake-alt"}),
            sites=sites,
        ),
        claude=claude,
    )


@pytest.mark.asyncio
async def test_engine_routes_to_tiered_strategy() -> None:
    claude = _FakeClaude(responses={"fake-fast": [_Verdict(label="ready", confidence=0.95)]})
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="tiered_confidence",
                primary="fast",
                escalation=["reasoning_anthropic"],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    result = await engine.decide(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )
    assert result.verdict.label == "ready"
    assert result.trace.strategy == "tiered_confidence"


@pytest.mark.asyncio
async def test_engine_routes_to_always_two_models() -> None:
    claude = _FakeClaude(
        responses={
            "fake-strong": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="ready", confidence=0.85)],
        },
    )
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="always_two_models",
                primary="reasoning_anthropic",
                escalation=["reasoning_alt"],
                confidence_threshold=0.7,
                agreement_fields=["label"],
            ),
        },
    )
    result = await engine.decide(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )
    assert result.verdict.label == "ready"
    assert result.trace.strategy == "always_two_models"


@pytest.mark.asyncio
async def test_engine_raises_for_unknown_site() -> None:
    engine = _engine(_FakeClaude(), sites={})
    with pytest.raises(KeyError, match="unknown_site"):
        await engine.decide(
            site_name="unknown_site",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )


@pytest.mark.asyncio
async def test_engine_propagates_consensus_unavailable() -> None:
    from caretaker.llm.claude import StructuredCompleteError

    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    engine = _engine(
        claude,
        sites={
            "readiness": SiteConfig(
                strategy="tiered_confidence",
                primary="fast",
                escalation=["reasoning_anthropic"],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    with pytest.raises(ConsensusUnavailable):
        await engine.decide(
            site_name="readiness",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )


@pytest.mark.asyncio
async def test_engine_raises_for_unknown_strategy() -> None:
    engine = _engine(
        _FakeClaude(),
        sites={
            "readiness": SiteConfig(
                strategy="unknown_strategy",  # type: ignore[arg-type]
                primary="fast",
                escalation=[],
                confidence_threshold=0.7,
                agreement_fields=[],
            ),
        },
    )
    with pytest.raises(ValueError, match="unknown_strategy"):
        await engine.decide(
            site_name="readiness",
            schema=_Verdict,
            system_prompt="sys",
            user_prompt="user",
            feature="readiness",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_engine.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the engine module**

Create `src/caretaker/consensus/engine.py`:

```python
"""ConsensusEngine — public API for tiered-consensus decisions."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusResult
from caretaker.consensus.strategies import (
    AlwaysTwoModels,
    Strategy,
    StrategyContext,
    TieredConfidence,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseModel")

StrategyName = Literal["tiered_confidence", "always_two_models"]


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """Per-decision-site engine configuration.

    Built once per orchestrator startup from
    :class:`~caretaker.config.ConsensusDomainConfig` and stored on
    :class:`EngineConfig.sites` keyed by site name.
    """

    strategy: StrategyName
    primary: str
    escalation: list[str]
    confidence_threshold: float
    agreement_fields: list[str]


@dataclass
class EngineConfig:
    """Top-level engine configuration."""

    pool: ProviderPool
    sites: dict[str, SiteConfig] = field(default_factory=dict)


# Registry — maps strategy name → constructor. Adding a new strategy is
# (a) implement it in strategies.py, (b) add an entry here.
_STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "tiered_confidence": TieredConfidence,
    "always_two_models": AlwaysTwoModels,
}


class ConsensusEngine:
    """Routes a decision to the configured strategy and returns the verdict."""

    def __init__(self, *, config: EngineConfig, claude: "ClaudeClient") -> None:
        self._config = config
        self._claude = claude

    async def decide(
        self,
        *,
        site_name: str,
        schema: type[T],
        system_prompt: str,
        user_prompt: str,
        feature: str,
        max_tokens: int = 2000,
    ) -> ConsensusResult[T]:
        """Run the configured strategy for ``site_name`` and return the result.

        Raises:
            KeyError: ``site_name`` is not configured in :attr:`EngineConfig.sites`.
            ValueError: configured strategy name is unknown.
            ConsensusUnavailable: every model attempt errored.
        """
        try:
            site = self._config.sites[site_name]
        except KeyError as exc:
            raise KeyError(
                f"consensus engine has no configuration for site {site_name!r}; "
                f"known sites: {sorted(self._config.sites)}"
            ) from exc

        strategy_cls = _STRATEGY_REGISTRY.get(site.strategy)
        if strategy_cls is None:
            raise ValueError(
                f"unknown consensus strategy {site.strategy!r} for site {site_name!r}; "
                f"known strategies: {sorted(_STRATEGY_REGISTRY)}"
            )

        ctx = StrategyContext(
            site_name=site_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            feature=feature,
            primary=site.primary,
            escalation=site.escalation,
            confidence_threshold=site.confidence_threshold,
            agreement_fields=site.agreement_fields,
            pool=self._config.pool,
            claude=self._claude,
            max_tokens=max_tokens,
        )
        return await strategy_cls().run(ctx)


__all__ = [
    "ConsensusEngine",
    "EngineConfig",
    "SiteConfig",
    "StrategyName",
]
```

Update `src/caretaker/consensus/__init__.py`:

```python
"""Caretaker LLM consensus engine.

See ``docs/plans/2026-04-30-llm-consensus-engine-design.md`` for the design.
"""

from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt

__all__ = [
    "ConsensusEngine",
    "ConsensusResult",
    "ConsensusTrace",
    "ConsensusUnavailable",
    "EngineConfig",
    "ModelAttempt",
    "SiteConfig",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_engine.py -v`
Expected: PASS — all five engine tests green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/engine.py src/caretaker/consensus/__init__.py tests/test_consensus_engine.py
git commit -m "$(cat <<'EOF'
feat(consensus): ConsensusEngine.decide public API

Routes a decision to the configured strategy by site name. Strategy
registry is a single dict so adding a third strategy in a follow-up is
a one-line change.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Process-wide engine holder

**Files:**
- Create: `src/caretaker/consensus/active.py`
- Test: `tests/test_consensus_active.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_active.py`:

```python
"""Tests for the process-wide ConsensusEngine holder."""

from __future__ import annotations

from caretaker.consensus import active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig
from caretaker.consensus.provider_pool import ProviderPool


def _engine() -> ConsensusEngine:
    pool = ProviderPool({"fast": "fake-fast"})
    return ConsensusEngine(
        config=EngineConfig(pool=pool, sites={}),
        claude=object(),  # type: ignore[arg-type]
    )


def test_get_returns_none_when_unconfigured() -> None:
    active.reset_for_tests()
    assert active.get_active_engine() is None


def test_configure_installs_engine() -> None:
    active.reset_for_tests()
    engine = _engine()
    active.configure(engine)
    assert active.get_active_engine() is engine


def test_reset_clears_engine() -> None:
    active.configure(_engine())
    active.reset_for_tests()
    assert active.get_active_engine() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_active.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the active module**

Create `src/caretaker/consensus/active.py`:

```python
"""Process-wide :class:`ConsensusEngine` holder.

Mirrors :mod:`caretaker.evolution.shadow_config` — orchestrator startup and
the FastAPI lifespan hook call :func:`configure` once with the constructed
engine; call sites read it via :func:`get_active_engine`.

Tests use :func:`reset_for_tests` between cases.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.consensus.engine import ConsensusEngine

_lock = threading.Lock()
_active: "ConsensusEngine | None" = None


def configure(engine: "ConsensusEngine") -> None:
    """Install the active engine. Idempotent."""
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = engine


def get_active_engine() -> "ConsensusEngine | None":
    """Return the installed engine, or ``None`` when unconfigured."""
    with _lock:
        return _active


def reset_for_tests() -> None:
    """Clear the active engine. Used by test fixtures."""
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = None


__all__ = ["configure", "get_active_engine", "reset_for_tests"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_active.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/active.py tests/test_consensus_active.py
git commit -m "$(cat <<'EOF'
feat(consensus): process-wide engine holder

Mirrors evolution/shadow_config — orchestrator installs engine once;
call sites read it via get_active_engine().

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: ModelPoolConfig in LLMConfig

**Files:**
- Modify: `src/caretaker/config.py`
- Test: `tests/test_consensus_config.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_config.py`:

```python
"""Tests for new consensus-related config models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from caretaker.config import LLMConfig, ModelPoolConfig


def test_model_pool_config_defaults_empty() -> None:
    cfg = ModelPoolConfig()
    assert cfg.pool == {}


def test_model_pool_config_accepts_tags() -> None:
    cfg = ModelPoolConfig(pool={"fast": "haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"})
    assert cfg.pool["fast"] == "haiku-4-5"


def test_llm_config_has_model_pool_default() -> None:
    cfg = LLMConfig()
    assert cfg.model_pool.pool == {}


def test_llm_config_accepts_pool() -> None:
    cfg = LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "haiku-4-5"}))
    assert cfg.model_pool.pool["fast"] == "haiku-4-5"


def test_model_pool_rejects_empty_value() -> None:
    with pytest.raises(ValidationError):
        ModelPoolConfig(pool={"fast": ""})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModelPoolConfig'`.

- [ ] **Step 3: Add ModelPoolConfig to config.py**

In `src/caretaker/config.py`, just before the `class LLMConfig(StrictBaseModel):` line (around line 338), add:

```python
class ModelPoolConfig(StrictBaseModel):
    """Capability-tag → concrete-model registry consumed by the consensus engine.

    Tags are operator-defined; common values: ``fast``, ``reasoning_anthropic``,
    ``reasoning_alt``, ``cheap``. Per-site
    :class:`ConsensusDomainConfig.primary` / ``escalation`` accept either a
    tag or a literal model string accepted by the LLM router.

    The pool stays empty by default — sites that don't opt into consensus
    never look at it.
    """

    pool: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapping of capability tag → concrete model string. Every value "
            "must be a non-empty string accepted by the LLM router (e.g. "
            "'claude-sonnet-4-6', 'openai/gpt-4o', 'azure_ai/gpt-4o')."
        ),
    )

    @field_validator("pool")
    @classmethod
    def _no_empty_values(cls, value: dict[str, str]) -> dict[str, str]:
        for tag, model in value.items():
            if not isinstance(model, str) or not model:
                raise ValueError(
                    f"pool tag {tag!r} maps to invalid value {model!r}; "
                    "every tag must resolve to a non-empty model string"
                )
        return value
```

Add `field_validator` to the existing pydantic imports at the top of `config.py`:

```python
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
```

In `class LLMConfig(StrictBaseModel):` (the body), add a new field after `fallback_models`:

```python
    # Capability-tag → model registry consumed by the consensus engine.
    # Empty by default; populated when a site opts into consensus.
    model_pool: ModelPoolConfig = Field(default_factory=ModelPoolConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_config.py::test_model_pool_config_defaults_empty tests/test_consensus_config.py::test_model_pool_config_accepts_tags tests/test_consensus_config.py::test_llm_config_has_model_pool_default tests/test_consensus_config.py::test_llm_config_accepts_pool tests/test_consensus_config.py::test_model_pool_rejects_empty_value -v`
Expected: PASS.

- [ ] **Step 5: Run the full config test suite to confirm no regressions**

Run: `uv run pytest tests/test_consensus_config.py -v && uv run pytest tests/ -k 'config' -v`
Expected: PASS for the new tests; no regressions in the broader config tests.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/config.py tests/test_consensus_config.py
git commit -m "$(cat <<'EOF'
feat(config): ModelPoolConfig on LLMConfig

Capability-tag → concrete-model registry that the consensus engine
resolves at call time. Empty by default; populated when a site opts in.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: ConsensusDomainConfig with validators

**Files:**
- Modify: `src/caretaker/config.py`
- Test: `tests/test_consensus_config.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consensus_config.py`:

```python
from caretaker.config import ConsensusDomainConfig


def test_consensus_domain_defaults() -> None:
    cfg = ConsensusDomainConfig()
    assert cfg.strategy == "tiered_confidence"
    assert cfg.primary == "fast"
    assert cfg.escalation == ["reasoning_anthropic"]
    assert cfg.confidence_threshold == 0.7
    assert cfg.agreement_fields == []


def test_consensus_domain_accepts_always_two_with_distinct_escalation() -> None:
    cfg = ConsensusDomainConfig(
        strategy="always_two_models",
        primary="reasoning_anthropic",
        escalation=["reasoning_alt"],
        agreement_fields=["verdict"],
    )
    assert cfg.strategy == "always_two_models"


def test_consensus_domain_rejects_always_two_without_escalation() -> None:
    with pytest.raises(ValidationError, match="escalation"):
        ConsensusDomainConfig(
            strategy="always_two_models",
            primary="reasoning_anthropic",
            escalation=[],
        )


def test_consensus_domain_rejects_always_two_with_same_primary_in_escalation() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        ConsensusDomainConfig(
            strategy="always_two_models",
            primary="reasoning_anthropic",
            escalation=["reasoning_anthropic"],
        )


def test_confidence_threshold_bounded() -> None:
    with pytest.raises(ValidationError):
        ConsensusDomainConfig(confidence_threshold=1.5)
    with pytest.raises(ValidationError):
        ConsensusDomainConfig(confidence_threshold=-0.1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'ConsensusDomainConfig'`.

- [ ] **Step 3: Add ConsensusDomainConfig to config.py**

In `src/caretaker/config.py`, just before the existing `class AgenticDomainConfig(StrictBaseModel):` (around line 1397), add:

```python
class ConsensusDomainConfig(StrictBaseModel):
    """Per-decision-site consensus engine configuration.

    Attached as an optional field to :class:`AgenticDomainConfig`. When
    ``None``, the existing single-model path runs (no engine involved).
    Sites opt in by setting this in YAML.

    Tag values resolve through :class:`LLMConfig.model_pool`. Literal model
    strings (anything not a known tag) pass through unchanged to the LLM
    router.
    """

    strategy: Literal["tiered_confidence", "always_two_models"] = "tiered_confidence"
    primary: str = Field(
        default="fast",
        description="Capability tag or literal model string for the primary call.",
    )
    escalation: list[str] = Field(
        default_factory=lambda: ["reasoning_anthropic"],
        description=(
            "Ordered tags/literals for escalation. TieredConfidence consults "
            "every entry on low-confidence; AlwaysTwoModels uses [0] as the "
            "second voter and [1:] as tiebreakers."
        ),
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="TieredConfidence escalates when verdict.confidence < this.",
    )
    agreement_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Field names compared for AlwaysTwoModels agreement. Empty list "
            "means compare full verdicts via ==. For readiness, set to "
            "['verdict'] so only the closed-enum field has to match."
        ),
    )

    @model_validator(mode="after")
    def _validate_strategy_specific(self) -> "ConsensusDomainConfig":
        if self.strategy == "always_two_models":
            if not self.escalation:
                raise ValueError(
                    "always_two_models requires escalation[0] (no second model configured)"
                )
            if self.escalation[0] == self.primary:
                raise ValueError(
                    f"always_two_models requires escalation[0] ({self.escalation[0]!r}) "
                    f"to be distinct from primary ({self.primary!r}); use a different "
                    "tag or literal model so the two-model gate consults distinct models"
                )
        return self
```

Add `model_validator` to the pydantic imports at the top of `config.py`:

```python
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/config.py tests/test_consensus_config.py
git commit -m "$(cat <<'EOF'
feat(config): ConsensusDomainConfig with strategy validators

Per-site config carrying strategy, primary tag, escalation list,
confidence threshold, and agreement fields. Validates that
always_two_models has a distinct escalation[0].

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: AgenticDomainConfig.consensus + AgenticConfig.size_classifier

**Files:**
- Modify: `src/caretaker/config.py`
- Test: `tests/test_consensus_config.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_consensus_config.py`:

```python
from caretaker.config import AgenticConfig, AgenticDomainConfig


def test_agentic_domain_consensus_default_none() -> None:
    cfg = AgenticDomainConfig()
    assert cfg.consensus is None


def test_agentic_domain_accepts_consensus() -> None:
    consensus = ConsensusDomainConfig(strategy="tiered_confidence")
    cfg = AgenticDomainConfig(consensus=consensus)
    assert cfg.consensus is consensus


def test_agentic_config_has_size_classifier_slot() -> None:
    cfg = AgenticConfig()
    assert isinstance(cfg.size_classifier, AgenticDomainConfig)
    assert cfg.size_classifier.mode == "off"
    assert cfg.size_classifier.consensus is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_config.py -v`
Expected: FAIL — `AttributeError: 'AgenticDomainConfig' object has no attribute 'consensus'` (or `AgenticConfig has no field 'size_classifier'`).

- [ ] **Step 3: Extend AgenticDomainConfig + AgenticConfig**

In `src/caretaker/config.py`, in `class AgenticDomainConfig(StrictBaseModel):`, add a new field after `max_tokens_override`:

```python
    # Optional consensus engine config. When set, the site routes its LLM
    # path through the engine; when None, the existing single-model path
    # (claude.structured_complete) runs unchanged.
    consensus: ConsensusDomainConfig | None = None
```

In `class AgenticConfig(StrictBaseModel):`, add a new field after `crystallizer_category`:

```python
    # Foundry's pre/post-flight sizing gate. Today the gate is a pure
    # heuristic (file count + line count). With a non-None ``consensus``
    # field, borderline cases consult the engine to judge whether a diff
    # in the gray zone is mechanical (route to Foundry) or genuinely
    # complex (escalate to Copilot).
    size_classifier: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_config.py -v`
Expected: PASS — all consensus config tests.

- [ ] **Step 5: Run any existing config tests for regressions**

Run: `uv run pytest tests/ -k 'config or shadow_config' -v`
Expected: PASS — no regressions on the existing config and shadow_config tests.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/config.py tests/test_consensus_config.py
git commit -m "$(cat <<'EOF'
feat(config): per-site consensus + size_classifier slot on AgenticConfig

AgenticDomainConfig.consensus is the per-site opt-in for the consensus
engine; None preserves the existing single-model path. New
AgenticConfig.size_classifier slot gives Foundry's sizing gate a
per-site config surface (no slot today — it's a pure heuristic site).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: ShadowDecisionRecord.consensus_trace_json field

**Files:**
- Modify: `src/caretaker/evolution/shadow.py`
- Test: `tests/test_shadow_decorator.py` (extend) or create `tests/test_shadow_consensus_trace.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_shadow_consensus_trace.py`:

```python
"""Test that ShadowDecisionRecord carries an optional consensus_trace_json."""

from __future__ import annotations

from datetime import UTC, datetime

from caretaker.evolution.shadow import ShadowDecisionRecord


def test_shadow_record_has_consensus_trace_field() -> None:
    record = ShadowDecisionRecord(
        id="abc",
        name="readiness",
        run_at=datetime.now(UTC),
        outcome="enforced_candidate",
        mode="enforce",
        legacy_verdict_json="null",
        candidate_verdict_json='{"verdict":"ready"}',
        consensus_trace_json='{"strategy":"tiered_confidence","attempts":[],"escalated":false,"final_model":"x"}',
    )
    assert record.consensus_trace_json is not None


def test_shadow_record_consensus_trace_defaults_none() -> None:
    record = ShadowDecisionRecord(
        id="abc",
        name="readiness",
        run_at=datetime.now(UTC),
        outcome="agree",
        mode="shadow",
        legacy_verdict_json="null",
    )
    assert record.consensus_trace_json is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_shadow_consensus_trace.py -v`
Expected: FAIL — `ValidationError: extra fields not permitted` (because `ConfigDict(extra="forbid")`).

- [ ] **Step 3: Add the field to ShadowDecisionRecord**

In `src/caretaker/evolution/shadow.py`, in `class ShadowDecisionRecord(BaseModel):` (around line 116), after the `candidate_model` field (line ~189), add:

```python
    consensus_trace_json: str | None = Field(
        default=None,
        description=(
            "JSON-serialised :class:`caretaker.consensus.ConsensusTrace` for "
            "decisions that ran through the consensus engine. ``None`` for "
            "legacy-only decisions and for sites where ``AgenticDomainConfig."
            "consensus`` is None."
        ),
    )
```

Also update the `properties` dict in `write_shadow_decision` (around line 215) so the trace is persisted to Neo4j:

```python
    properties: dict[str, Any] = {
        "name": record.name,
        "repo_slug": record.repo_slug,
        "run_at": record.run_at.isoformat(),
        "outcome": record.outcome,
        "mode": record.mode,
        "legacy_verdict_json": record.legacy_verdict_json,
        "candidate_verdict_json": record.candidate_verdict_json or "",
        "disagreement_reason": record.disagreement_reason or "",
        "context_json": record.context_json,
        "legacy_model": record.legacy_model or "",
        "candidate_model": record.candidate_model or "",
        # Empty string when None so Neo4j storage doesn't have to special-case
        # NULLs; admin API normalises empty string back to None on read.
        "consensus_trace_json": record.consensus_trace_json or "",
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_shadow_consensus_trace.py -v && uv run pytest tests/test_shadow_decorator.py -v`
Expected: PASS — new tests green; no regressions on existing shadow decorator tests.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/evolution/shadow.py tests/test_shadow_consensus_trace.py
git commit -m "$(cat <<'EOF'
feat(evolution): ShadowDecisionRecord.consensus_trace_json

Optional per-decision audit trail field that the @shadow_decision
wrapper populates when the consensus engine ran. Persisted to Neo4j
properties so the admin API can surface the trace.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Wire engine into evaluate_pr_readiness_llm

**Files:**
- Modify: `src/caretaker/pr_agent/readiness_llm.py`
- Test: `tests/test_pr_readiness_consensus.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_pr_readiness_consensus.py`:

```python
"""Integration: evaluate_pr_readiness_llm uses the consensus engine when configured."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from caretaker.consensus import active as consensus_active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable
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
            pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
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

    # Build a minimal-ish ReadinessContext. For the integration test we
    # patch the prompt-building functions to bypass GitHub model construction.
    from caretaker.pr_agent.readiness_llm import ReadinessContext

    ctx = ReadinessContext.__new__(ReadinessContext)  # avoid pydantic field requirements
    object.__setattr__(ctx, "pr", _FakePR())
    object.__setattr__(ctx, "check_runs", [])
    object.__setattr__(ctx, "reviews", [])
    object.__setattr__(ctx, "memory", None)
    object.__setattr__(ctx, "memory_excerpt", "")
    object.__setattr__(ctx, "repo_slug", "owner/repo")
    object.__setattr__(ctx, "collaborator_count", 1)

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    assert verdict is not None
    assert verdict.verdict == "ready"
    # Both engine models were called — claude.structured_complete was NOT
    # called directly by evaluate_pr_readiness_llm (its bypass was via the
    # engine, which still uses claude.structured_complete under the hood,
    # so calls list contains the per-model engine calls).
    assert "fake-anthropic" in claude.calls
    assert "fake-alt" in claude.calls


@pytest.mark.asyncio
async def test_readiness_falls_back_when_engine_unavailable() -> None:
    """Engine raising ConsensusUnavailable returns None so shadow falls through."""
    from caretaker.llm.claude import StructuredCompleteError
    from caretaker.pr_agent.readiness_llm import ReadinessContext, evaluate_pr_readiness_llm

    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-anthropic": [err], "fake-alt": [err]})
    engine = ConsensusEngine(
        config=EngineConfig(
            pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
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

    ctx = ReadinessContext.__new__(ReadinessContext)
    object.__setattr__(ctx, "pr", _FakePR())
    object.__setattr__(ctx, "check_runs", [])
    object.__setattr__(ctx, "reviews", [])
    object.__setattr__(ctx, "memory", None)
    object.__setattr__(ctx, "memory_excerpt", "")
    object.__setattr__(ctx, "repo_slug", "owner/repo")
    object.__setattr__(ctx, "collaborator_count", 1)

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    # Per spec: engine unavailable → return None so @shadow_decision falls
    # through to the legacy heuristic.
    assert verdict is None


@pytest.mark.asyncio
async def test_readiness_uses_direct_call_when_engine_inactive() -> None:
    """When no engine is active, falls back to the direct claude.structured_complete path."""
    from caretaker.pr_agent.readiness_llm import ReadinessContext, evaluate_pr_readiness_llm

    consensus_active.reset_for_tests()  # ensure no engine
    claude = _FakeClaude(responses={"<default>": [_readiness("not_ready", confidence=0.6)]})

    ctx = ReadinessContext.__new__(ReadinessContext)
    object.__setattr__(ctx, "pr", _FakePR())
    object.__setattr__(ctx, "check_runs", [])
    object.__setattr__(ctx, "reviews", [])
    object.__setattr__(ctx, "memory", None)
    object.__setattr__(ctx, "memory_excerpt", "")
    object.__setattr__(ctx, "repo_slug", "owner/repo")
    object.__setattr__(ctx, "collaborator_count", 1)

    verdict = await evaluate_pr_readiness_llm(ctx, claude=claude)
    assert verdict is not None
    assert verdict.verdict == "not_ready"
    # Single direct call, model=None.
    assert claude.calls == ["<default>"]


# ── Test fixtures ─────────────────────────────────────────────────────────


@dataclass
class _FakePR:
    number: int = 1
    title: str = "Test PR"
    body: str | None = "Body"
    draft: bool = False
    mergeable: bool | None = True
    labels: list[Any] = field(default_factory=list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pr_readiness_consensus.py -v`
Expected: FAIL — the existing `evaluate_pr_readiness_llm` doesn't know about the engine yet.

- [ ] **Step 3: Wire engine into evaluate_pr_readiness_llm**

Read `src/caretaker/pr_agent/readiness_llm.py` end-to-end first. Then in the function `evaluate_pr_readiness_llm`, replace the body's `await claude.structured_complete(...)` call with an engine-aware version. The function signature stays unchanged.

Concretely, find the existing function body that ends with something like:

```python
    try:
        return await claude.structured_complete(
            prompt,
            schema=Readiness,
            feature="readiness",
            system=_READINESS_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info("evaluate_pr_readiness_llm failed: %s", exc)
        return None
```

Replace with:

```python
    from caretaker.consensus import active as consensus_active
    from caretaker.consensus.result import ConsensusUnavailable

    engine = consensus_active.get_active_engine()
    if engine is not None and "readiness" in engine._config.sites:  # noqa: SLF001
        try:
            result = await engine.decide(
                site_name="readiness",
                schema=Readiness,
                system_prompt=_READINESS_SYSTEM_PROMPT,
                user_prompt=prompt,
                feature="readiness",
            )
        except ConsensusUnavailable as exc:
            logger.info("evaluate_pr_readiness_llm: consensus unavailable: %s", exc)
            return None
        return result.verdict

    # No engine configured — direct single-model path (existing behaviour).
    try:
        return await claude.structured_complete(
            prompt,
            schema=Readiness,
            feature="readiness",
            system=_READINESS_SYSTEM_PROMPT,
        )
    except StructuredCompleteError as exc:
        logger.info("evaluate_pr_readiness_llm failed: %s", exc)
        return None
```

(Note: the `engine._config.sites` check is a private-attribute read but acceptable here because the engine and call site are in the same package. A public `engine.has_site(name)` helper is a fine cleanup if the reviewer asks for it — add the helper to `engine.py` and update the call site if so.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pr_readiness_consensus.py -v`
Expected: PASS — all three integration tests green.

- [ ] **Step 5: Run the broader readiness test suite to confirm no regressions**

Run: `uv run pytest tests/ -k 'readiness' -v`
Expected: PASS — no regressions on existing readiness tests.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/pr_agent/readiness_llm.py tests/test_pr_readiness_consensus.py
git commit -m "$(cat <<'EOF'
feat(pr_agent): readiness routes through consensus engine when active

evaluate_pr_readiness_llm checks for a process-wide engine; when present
and configured for ``readiness``, runs through the engine. When the
engine is inactive or has no readiness site, falls back to the existing
direct claude.structured_complete path. ConsensusUnavailable returns
None so @shadow_decision falls through to the legacy heuristic.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Add async decide_pre / decide_post to size_classifier

**Files:**
- Modify: `src/caretaker/foundry/size_classifier.py`
- Test: `tests/test_size_classifier_consensus.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_size_classifier_consensus.py`:

```python
"""Tests for the size_classifier hybrid floor/ceiling + LLM borderline path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field

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
            "fake-fast": [SizeVerdict(decision=Decision.ROUTE_FOUNDRY, confidence=0.85, reason="mechanical refactor")],
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_size_classifier_consensus.py -v`
Expected: FAIL — `ImportError: cannot import name 'SizeVerdict'` (or `decide_pre`).

- [ ] **Step 3: Add SizeVerdict + decide_pre / decide_post to size_classifier.py**

In `src/caretaker/foundry/size_classifier.py`, append the new types and functions. **Keep the existing `pre_flight` and `post_flight` synchronous functions exactly as-is** — they remain the deterministic floor/ceiling that `decide_pre` / `decide_post` delegate to.

```python
# ──────────────────────────────────────────────────────────────────────────
# Hybrid floor/ceiling decision API
# ──────────────────────────────────────────────────────────────────────────


from pydantic import BaseModel, Field


class SizeVerdict(BaseModel):
    """LLM-emitted verdict in the borderline zone.

    Schema kept tight: closed enum + confidence + free-text reason. The
    consensus engine compares verdicts on ``decision`` for AlwaysTwoModels
    if a site ever swaps strategies.
    """

    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=300)


_SIZE_SYSTEM_PROMPT = """\
You are caretaker's size_classifier. Given a Foundry task summary,
decide whether the diff/error is small enough to route to the
Foundry executor (ROUTE_FOUNDRY) or should escalate to Copilot
(ESCALATE_COPILOT).

Rules:
- Mechanical refactors (rename, lint fix, type tightening) — ROUTE_FOUNDRY
  even at higher line counts.
- Genuinely complex logic changes, multi-package refactors, or anything
  touching auth/migrations/public APIs — ESCALATE_COPILOT.
- ``confidence`` is your self-assessed probability the verdict is correct.
- ``reason`` must be a single line no longer than 300 characters.
"""


async def decide_pre(
    *,
    task_type: str,
    allowed_task_types: list[str],
    head_repo_full_name: str | None,
    base_repo_full_name: str | None,
    route_same_repo_only: bool,
    error_output: str,
    max_error_output_chars: int = 16_000,
    borderline_low_error_chars: int = 4_000,
    borderline_high_error_chars: int = 12_000,
) -> ClassifierResult:
    """Async pre-flight gate with deterministic floor/ceiling.

    Below ``borderline_low_error_chars`` → always ROUTE_FOUNDRY.
    Above ``borderline_high_error_chars`` (or any other deterministic
    rejection like task-type mismatch / fork PR) → ESCALATE_COPILOT.
    Between → consult the consensus engine (when active).
    """
    # Hard rejections — task type, fork PR — short-circuit.
    legacy = pre_flight(
        task_type=task_type,
        allowed_task_types=allowed_task_types,
        head_repo_full_name=head_repo_full_name,
        base_repo_full_name=base_repo_full_name,
        route_same_repo_only=route_same_repo_only,
        error_output=error_output,
        max_error_output_chars=max_error_output_chars,
    )
    if legacy.decision != Decision.ROUTE_FOUNDRY:
        # Above ceiling or hard reject — return as-is.
        return legacy

    error_chars = len(error_output or "")
    if error_chars < borderline_low_error_chars:
        return legacy  # well under low band — fast path stays

    if error_chars > borderline_high_error_chars:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"error_output is {error_chars} chars (> borderline high "
                f"{borderline_high_error_chars}); escalating without engine consult"
            ),
        )

    # Borderline — consult engine if active.
    return await _engine_consult_or_fallback(
        site="size_classifier",
        prompt=(
            f"Foundry pre-flight summary:\n"
            f"- task_type: {task_type}\n"
            f"- error_output_chars: {error_chars}\n"
            f"- error_output_excerpt:\n{(error_output or '')[:1000]}\n"
        ),
        fallback=legacy,
    )


async def decide_post(
    *,
    files_changed: int,
    insertions: int,
    deletions: int,
    max_files_touched: int,
    max_diff_lines: int,
    borderline_low_files: int = 3,
    borderline_high_files: int = 10,
    borderline_low_lines: int = 100,
    borderline_high_lines: int = 300,
) -> ClassifierResult:
    """Async post-flight gate with deterministic floor/ceiling.

    Both files-changed and total-lines have their own borderline bands.
    A diff is "borderline" if **either** dimension is in its band — gives
    the engine a chance to weigh "lots of small files" vs "few large
    files" cases.
    """
    legacy = post_flight(
        files_changed=files_changed,
        insertions=insertions,
        deletions=deletions,
        max_files_touched=max_files_touched,
        max_diff_lines=max_diff_lines,
    )
    if legacy.decision == Decision.ESCALATE_COPILOT:
        # Above the hard ceiling — never consult the engine; it can't
        # rescue a 25-file diff.
        return legacy

    total_lines = insertions + deletions
    files_borderline = borderline_low_files <= files_changed <= borderline_high_files
    lines_borderline = borderline_low_lines <= total_lines <= borderline_high_lines

    if files_changed < borderline_low_files and total_lines < borderline_low_lines:
        # Well under both floors — fast path.
        return legacy

    if files_changed > borderline_high_files or total_lines > borderline_high_lines:
        return ClassifierResult(
            decision=Decision.ESCALATE_COPILOT,
            reason=(
                f"borderline ceiling exceeded: files={files_changed} "
                f"lines={total_lines}; escalating without engine consult"
            ),
        )

    if not (files_borderline or lines_borderline):
        return legacy  # in mixed gray-zones we trust the legacy gate

    return await _engine_consult_or_fallback(
        site="size_classifier",
        prompt=(
            f"Foundry post-flight diff summary:\n"
            f"- files_changed: {files_changed}\n"
            f"- insertions: {insertions}\n"
            f"- deletions: {deletions}\n"
            f"- total_lines: {total_lines}\n"
        ),
        fallback=legacy,
    )


async def _engine_consult_or_fallback(
    *,
    site: str,
    prompt: str,
    fallback: ClassifierResult,
) -> ClassifierResult:
    """Call the consensus engine when active; on failure return ``fallback``."""
    from caretaker.consensus import active as consensus_active
    from caretaker.consensus.result import ConsensusUnavailable

    engine = consensus_active.get_active_engine()
    if engine is None or site not in engine._config.sites:  # noqa: SLF001
        return fallback

    try:
        result = await engine.decide(
            site_name=site,
            schema=SizeVerdict,
            system_prompt=_SIZE_SYSTEM_PROMPT,
            user_prompt=prompt,
            feature="size_classifier",
        )
    except ConsensusUnavailable:
        return fallback

    return ClassifierResult(
        decision=result.verdict.decision,
        reason=f"engine: {result.verdict.reason}",
    )
```

(Note: the `engine._config.sites` private read mirrors the readiness call site. Same recommendation: a public `engine.has_site(name)` helper if the reviewer asks. Keep the call sites consistent — fix or leave them both.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_size_classifier_consensus.py -v`
Expected: PASS — all six size_classifier tests green.

- [ ] **Step 5: Run the broader Foundry test suite to confirm no regressions**

Run: `uv run pytest tests/ -k 'foundry or size_classifier' -v`
Expected: PASS — no regressions on existing Foundry tests (they still call the synchronous `pre_flight` / `post_flight`).

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/foundry/size_classifier.py tests/test_size_classifier_consensus.py
git commit -m "$(cat <<'EOF'
feat(foundry): hybrid floor/ceiling + engine-consulted borderline zone

Introduces async decide_pre / decide_post that wrap the existing
deterministic pre_flight / post_flight: counts under the low band fast-path
to ROUTE_FOUNDRY, counts over the high band fast-path to ESCALATE_COPILOT,
borderline cases consult the consensus engine when active. Engine failure
falls back to the legacy count-gate verdict so the orchestrator never
wedges on a provider outage.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Update FoundryExecutor to await decide_pre / decide_post

**Files:**
- Modify: `src/caretaker/foundry/executor.py`
- Test: existing executor tests must still pass

- [ ] **Step 1: Read the existing call sites**

Run: `grep -n "pre_flight\|post_flight" src/caretaker/foundry/executor.py`

Expected: two call sites — one in `run` (around line 265) and one in `_run_locked` (around line 354).

- [ ] **Step 2: Update imports**

In `src/caretaker/foundry/executor.py`, replace:

```python
from caretaker.foundry.size_classifier import (
    ClassifierResult,
    Decision,
    post_flight,
    pre_flight,
)
```

with:

```python
from caretaker.foundry.size_classifier import (
    ClassifierResult,
    Decision,
    decide_post,
    decide_pre,
)
```

- [ ] **Step 3: Update the pre_flight call site**

In the `run` method (around line 265), replace:

```python
        pre = pre_flight(
            task_type=task.task_type.value,
            allowed_task_types=self._config.allowed_task_types,
            head_repo_full_name=head_repo,
            base_repo_full_name=base_repo,
            route_same_repo_only=self._config.route_same_repo_only,
            error_output=task.error_output,
        )
```

with:

```python
        pre = await decide_pre(
            task_type=task.task_type.value,
            allowed_task_types=self._config.allowed_task_types,
            head_repo_full_name=head_repo,
            base_repo_full_name=base_repo,
            route_same_repo_only=self._config.route_same_repo_only,
            error_output=task.error_output,
        )
```

- [ ] **Step 4: Update the post_flight call site**

In the `_run_locked` method (around line 354), replace:

```python
                post = post_flight(
                    files_changed=diff_stats["files_changed"],
                    insertions=diff_stats["insertions"],
                    deletions=diff_stats["deletions"],
                    max_files_touched=self._config.max_files_touched,
                    max_diff_lines=self._config.max_diff_lines,
                )
```

with:

```python
                post = await decide_post(
                    files_changed=diff_stats["files_changed"],
                    insertions=diff_stats["insertions"],
                    deletions=diff_stats["deletions"],
                    max_files_touched=self._config.max_files_touched,
                    max_diff_lines=self._config.max_diff_lines,
                )
```

- [ ] **Step 5: Run the executor test suite to confirm no regressions**

Run: `uv run pytest tests/ -k 'executor or foundry' -v`
Expected: PASS — existing tests still pass; the legacy `pre_flight` / `post_flight` synchronous functions remain untouched and still imported by tests directly.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/foundry/executor.py
git commit -m "$(cat <<'EOF'
feat(foundry): executor calls decide_pre/decide_post instead of pre_flight/post_flight

Switches the FoundryExecutor's two sizing-gate call sites from the
synchronous pre_flight/post_flight to the new async decide_pre/decide_post
so borderline cases can consult the consensus engine when active. The
legacy pure functions stay exported and remain the deterministic floor/
ceiling under the new wrappers.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Build engine at orchestrator startup; configure consensus.active

**Files:**
- Modify: `src/caretaker/orchestrator.py`
- Test: `tests/test_orchestrator_consensus.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_orchestrator_consensus.py`:

```python
"""Test that Orchestrator constructs and installs a ConsensusEngine when sites opt in."""

from __future__ import annotations

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.consensus import active as consensus_active


def test_orchestrator_init_installs_consensus_when_any_site_opts_in(tmp_path) -> None:
    """When at least one AgenticDomainConfig has consensus set, install the engine."""
    consensus_active.reset_for_tests()

    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={
                    "fast": "claude-haiku-4-5",
                    "reasoning_anthropic": "claude-sonnet-4-6",
                    "reasoning_alt": "openai/gpt-4o",
                },
            ),
        ),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="always_two_models",
                    primary="reasoning_anthropic",
                    escalation=["reasoning_alt"],
                    agreement_fields=["verdict"],
                ),
            ),
        ),
    )

    # Build via the orchestrator helper (rather than full __init__) — the
    # helper is the function under test.
    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is not None
    # Site appears in the engine config.
    assert "readiness" in engine._config.sites  # noqa: SLF001


def test_orchestrator_init_skips_consensus_when_no_site_opts_in() -> None:
    consensus_active.reset_for_tests()

    config = MaintainerConfig()  # all defaults — no consensus anywhere
    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is None


def test_orchestrator_init_size_classifier_consensus() -> None:
    consensus_active.reset_for_tests()

    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={"fast": "claude-haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"},
            ),
        ),
        agentic=AgenticConfig(
            size_classifier=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                ),
            ),
        ),
    )
    from caretaker.orchestrator import _build_consensus_engine

    engine = _build_consensus_engine(config)
    assert engine is not None
    assert "size_classifier" in engine._config.sites  # noqa: SLF001
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orchestrator_consensus.py -v`
Expected: FAIL — `ImportError: cannot import name '_build_consensus_engine'`.

- [ ] **Step 3: Add _build_consensus_engine helper + wire into Orchestrator.__init__**

In `src/caretaker/orchestrator.py`, near the other top-level helpers (search for `_build_executor_dispatcher`), add:

```python
def _build_consensus_engine(config: MaintainerConfig) -> "ConsensusEngine | None":
    """Build a ConsensusEngine from the configured sites, or return None.

    Walks every :class:`AgenticDomainConfig` on :class:`AgenticConfig` and
    collects the ones with a non-None ``consensus`` field. If none opt in,
    no engine is built and the existing single-model paths run unchanged.
    """
    from caretaker.consensus.engine import ConsensusEngine, EngineConfig, SiteConfig
    from caretaker.consensus.provider_pool import ProviderPool

    sites: dict[str, SiteConfig] = {}
    for site_name in (
        "readiness",
        "ci_triage",
        "review_classification",
        "issue_triage",
        "cascade",
        "stuck_pr",
        "bot_identity",
        "dispatch_guard",
        "executor_routing",
        "crystallizer_category",
        "size_classifier",
    ):
        domain = getattr(config.agentic, site_name, None)
        if domain is None:
            continue
        consensus_cfg = getattr(domain, "consensus", None)
        if consensus_cfg is None:
            continue
        sites[site_name] = SiteConfig(
            strategy=consensus_cfg.strategy,
            primary=consensus_cfg.primary,
            escalation=list(consensus_cfg.escalation),
            confidence_threshold=consensus_cfg.confidence_threshold,
            agreement_fields=list(consensus_cfg.agreement_fields),
        )

    if not sites:
        return None

    pool = ProviderPool(dict(config.llm.model_pool.pool))

    # ClaudeClient is constructed once via LLMRouter; reuse it.
    from caretaker.llm.claude import ClaudeClient

    claude = ClaudeClient(config=config.llm)
    return ConsensusEngine(config=EngineConfig(pool=pool, sites=sites), claude=claude)
```

In `class Orchestrator:`'s `__init__`, after `self._llm = LLMRouter(config.llm)` (around line 276), add:

```python
        # ── Consensus engine (Phase 3 of agentic migration) ────────────
        from caretaker.consensus import active as consensus_active
        from caretaker.evolution import shadow_config

        # Install the agentic config so @shadow_decision can resolve modes.
        # This was previously only set in tests; production runs need it too.
        shadow_config.configure_maintainer(config)

        self._consensus_engine = _build_consensus_engine(config)
        if self._consensus_engine is not None:
            consensus_active.configure(self._consensus_engine)
            logger.info(
                "Consensus engine active for sites: %s",
                sorted(self._consensus_engine._config.sites),  # noqa: SLF001
            )
        else:
            consensus_active.reset_for_tests()
```

Add the typing import at the top of the file:

```python
if TYPE_CHECKING:
    from caretaker.consensus.engine import ConsensusEngine
```

(Or omit the `TYPE_CHECKING` block — the helper's return type is a forward reference string already.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orchestrator_consensus.py -v`
Expected: PASS — three orchestrator-engine tests green.

- [ ] **Step 5: Run the orchestrator regression suite**

Run: `uv run pytest tests/test_orchestrator.py tests/ -k 'orchestrator' -v`
Expected: PASS — no regressions on existing orchestrator tests.

- [ ] **Step 6: Commit**

```bash
git add src/caretaker/orchestrator.py tests/test_orchestrator_consensus.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): build + install ConsensusEngine at startup

When any AgenticDomainConfig has a non-None consensus field, the
orchestrator constructs a ConsensusEngine and registers it on
caretaker.consensus.active. Also wires shadow_config.configure_maintainer
which was previously only configured in tests.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Observability counters

**Files:**
- Create: `src/caretaker/consensus/metrics.py`
- Modify: `src/caretaker/consensus/strategies.py` (increment counters)
- Modify: `src/caretaker/consensus/engine.py` (increment unavailable counter)
- Test: `tests/test_consensus_metrics.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_consensus_metrics.py`:

```python
"""Tests that consensus strategies increment Prometheus counters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, Field

from caretaker.consensus.metrics import (
    CONSENSUS_DECISIONS_TOTAL,
    CONSENSUS_DISAGREEMENT_TOTAL,
    CONSENSUS_UNAVAILABLE_TOTAL,
)
from caretaker.consensus.provider_pool import ProviderPool
from caretaker.consensus.result import ConsensusUnavailable
from caretaker.consensus.strategies import (
    AlwaysTwoModels,
    StrategyContext,
    TieredConfidence,
)
from caretaker.llm.claude import StructuredCompleteError


class _Verdict(BaseModel):
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)

    async def structured_complete(
        self, prompt: str, *, schema: type[Any], feature: str, system: str | None = None,
        model: str | None = None, max_retries: int | None = None, max_tokens: int = 2000,
    ) -> Any:
        queue = self.responses.get(model or "<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_tiered_increments_primary_shipped_counter() -> None:
    before = CONSENSUS_DECISIONS_TOTAL.labels(
        site="readiness", strategy="tiered_confidence", outcome="primary_shipped"
    )._value.get()
    claude = _FakeClaude(responses={"fake-fast": [_Verdict(label="ready", confidence=0.95)]})
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="fast",
        escalation=["reasoning_anthropic"],
        confidence_threshold=0.7,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )
    await TieredConfidence().run(ctx)
    after = CONSENSUS_DECISIONS_TOTAL.labels(
        site="readiness", strategy="tiered_confidence", outcome="primary_shipped"
    )._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_atm_increments_disagreement_counter() -> None:
    before = CONSENSUS_DISAGREEMENT_TOTAL.labels(site="readiness")._value.get()
    claude = _FakeClaude(
        responses={
            "fake-anthropic": [_Verdict(label="ready", confidence=0.9)],
            "fake-alt": [_Verdict(label="not_ready", confidence=0.85)],
            "literal-tiebreaker": [_Verdict(label="ready", confidence=0.7)],
        },
    )
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="reasoning_anthropic",
        escalation=["reasoning_alt", "literal-tiebreaker"],
        confidence_threshold=0.7,
        agreement_fields=["label"],
        pool=ProviderPool({"reasoning_anthropic": "fake-anthropic", "reasoning_alt": "fake-alt"}),
        claude=claude,
    )
    await AlwaysTwoModels().run(ctx)
    after = CONSENSUS_DISAGREEMENT_TOTAL.labels(site="readiness")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_unavailable_counter_increments_on_total_failure() -> None:
    before = CONSENSUS_UNAVAILABLE_TOTAL.labels(site="readiness")._value.get()
    err = StructuredCompleteError(raw_text="", validation_error=RuntimeError("nope"))
    claude = _FakeClaude(responses={"fake-fast": [err], "fake-strong": [err]})
    ctx = StrategyContext(
        site_name="readiness",
        schema=_Verdict,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
        primary="fast",
        escalation=["reasoning_anthropic"],
        confidence_threshold=0.7,
        agreement_fields=[],
        pool=ProviderPool({"fast": "fake-fast", "reasoning_anthropic": "fake-strong"}),
        claude=claude,
    )
    with pytest.raises(ConsensusUnavailable):
        await TieredConfidence().run(ctx)
    after = CONSENSUS_UNAVAILABLE_TOTAL.labels(site="readiness")._value.get()
    assert after == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_consensus_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: caretaker.consensus.metrics`.

- [ ] **Step 3: Create metrics module + wire counters into strategies**

Create `src/caretaker/consensus/metrics.py`:

```python
"""Prometheus counters/histograms for the consensus engine."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

from caretaker.observability.metrics import REGISTRY

CONSENSUS_DECISIONS_TOTAL = Counter(
    "caretaker_consensus_decisions_total",
    "Total consensus decisions per (site, strategy, outcome).",
    ["site", "strategy", "outcome"],
    registry=REGISTRY,
)

CONSENSUS_DISAGREEMENT_TOTAL = Counter(
    "caretaker_consensus_disagreement_total",
    "AlwaysTwoModels disagreements that triggered a tiebreaker, per site.",
    ["site"],
    registry=REGISTRY,
)

CONSENSUS_UNAVAILABLE_TOTAL = Counter(
    "caretaker_consensus_unavailable_total",
    "Decisions where every model attempt errored, per site.",
    ["site"],
    registry=REGISTRY,
)

CONSENSUS_DECISION_SECONDS = Histogram(
    "caretaker_consensus_decision_seconds",
    "End-to-end engine latency including all model calls, per (site, strategy).",
    ["site", "strategy"],
    registry=REGISTRY,
)


__all__ = [
    "CONSENSUS_DECISIONS_TOTAL",
    "CONSENSUS_DECISION_SECONDS",
    "CONSENSUS_DISAGREEMENT_TOTAL",
    "CONSENSUS_UNAVAILABLE_TOTAL",
]
```

In `src/caretaker/consensus/strategies.py`, increment the counters at the right places.

In `TieredConfidence.run`, replace the existing `return ConsensusResult(...)` blocks with versions that increment the outcome counter:

```python
        from caretaker.consensus.metrics import CONSENSUS_DECISIONS_TOTAL, CONSENSUS_UNAVAILABLE_TOTAL
```

(Add at the top of the file with the other imports.)

For the "primary above threshold" path:

```python
            CONSENSUS_DECISIONS_TOTAL.labels(
                site=ctx.site_name, strategy=self.name, outcome="primary_shipped"
            ).inc()
            return ConsensusResult(
                verdict=primary_verdict,
                trace=ConsensusTrace(...)
            )
```

For the "ConsensusUnavailable" path, just before the raise:

```python
            CONSENSUS_UNAVAILABLE_TOTAL.labels(site=ctx.site_name).inc()
            raise ConsensusUnavailable(...)
```

For the "escalation winner" path:

```python
            CONSENSUS_DECISIONS_TOTAL.labels(
                site=ctx.site_name, strategy=self.name, outcome="escalated"
            ).inc()
            return ConsensusResult(...)
```

Apply analogous increments to `AlwaysTwoModels.run`:

- Agreement path → `outcome="primary_shipped"`
- Disagreement → `CONSENSUS_DISAGREEMENT_TOTAL.labels(site=ctx.site_name).inc()` AND `outcome="tiebreaker_shipped"` on the final return
- Total failure → `CONSENSUS_UNAVAILABLE_TOTAL` + raise

Add to imports in `strategies.py`:

```python
from caretaker.consensus.metrics import (
    CONSENSUS_DECISIONS_TOTAL,
    CONSENSUS_DISAGREEMENT_TOTAL,
    CONSENSUS_UNAVAILABLE_TOTAL,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_metrics.py -v && uv run pytest tests/test_consensus_strategies.py -v`
Expected: PASS — metrics tests green; existing strategy tests still green.

- [ ] **Step 5: Commit**

```bash
git add src/caretaker/consensus/metrics.py src/caretaker/consensus/strategies.py tests/test_consensus_metrics.py
git commit -m "$(cat <<'EOF'
feat(consensus): Prometheus counters + histogram for decisions

Three Counters (decisions_total, disagreement_total, unavailable_total)
and one Histogram (decision_seconds). Strategies increment per-outcome.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Default config + doctor validation

**Files:**
- Modify: `caretaker_config.yaml.example` (find existing path; common location is repo root or `setup-templates/`)
- Modify: `src/caretaker/doctor.py`
- Test: `tests/test_doctor_consensus.py`

- [ ] **Step 1: Locate the example config file**

Run: `find . -name 'caretaker_config*.yaml*' -o -name '*example*.yaml' | grep -v node_modules | head -10`

Expected: a single example yaml. If multiple, pick the one referenced by `docs/configuration.md`.

- [ ] **Step 2: Add example consensus configuration**

Append (or insert in the appropriate section) to the example yaml:

```yaml
# ── LLM model pool (consensus engine) ─────────────────────────────────────
# Capability tags resolve to concrete model strings at decision time.
# Adding/removing tags here is the only change needed when a model
# is deprecated upstream.
llm:
  model_pool:
    pool:
      fast: claude-haiku-4-5
      reasoning_anthropic: claude-sonnet-4-6
      reasoning_alt: openai/gpt-4o
      cheap: claude-haiku-4-5

# ── Agentic decision sites (Phase 2 + consensus) ─────────────────────────
agentic:
  readiness:
    mode: enforce
    consensus:
      strategy: always_two_models
      primary: reasoning_anthropic
      escalation: [reasoning_alt]
      agreement_fields: [verdict]
  size_classifier:
    mode: enforce
    consensus:
      strategy: tiered_confidence
      primary: fast
      escalation: [reasoning_anthropic]
      confidence_threshold: 0.7
```

- [ ] **Step 3: Write the failing doctor test**

Create `tests/test_doctor_consensus.py`:

```python
"""Tests that doctor.py flags missing pool entries used by consensus configs."""

from __future__ import annotations

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.doctor import diagnose_consensus_config


def test_diagnose_passes_when_tags_present_in_pool() -> None:
    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={"fast": "claude-haiku-4-5", "reasoning_anthropic": "claude-sonnet-4-6"},
            ),
        ),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic"],
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert issues == []


def test_diagnose_flags_missing_tag_and_keeps_literal_pass_through() -> None:
    config = MaintainerConfig(
        llm=LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "claude-haiku-4-5"})),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["reasoning_anthropic_TYPO"],  # tag-shaped, not in pool
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert any("reasoning_anthropic_TYPO" in issue for issue in issues)


def test_diagnose_does_not_flag_literal_model_strings() -> None:
    """Literals (strings with '/' or starting with a known prefix) pass through."""
    config = MaintainerConfig(
        llm=LLMConfig(model_pool=ModelPoolConfig(pool={"fast": "claude-haiku-4-5"})),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="tiered_confidence",
                    primary="fast",
                    escalation=["openai/gpt-4o"],  # literal — pool miss is OK
                ),
            ),
        ),
    )
    issues = diagnose_consensus_config(config)
    assert issues == []
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor_consensus.py -v`
Expected: FAIL — `ImportError: cannot import name 'diagnose_consensus_config'`.

- [ ] **Step 5: Add diagnose_consensus_config to doctor.py**

In `src/caretaker/doctor.py`, add the function:

```python
def diagnose_consensus_config(config: "MaintainerConfig") -> list[str]:
    """Return human-readable issues with the consensus engine configuration.

    Each entry in the returned list is a single-sentence issue. Empty list
    means the consensus configuration is internally consistent. Caller
    decides whether to log warnings or fail-fast.

    Heuristic for "literal vs tag": a string containing ``/`` or matching
    a known LiteLLM provider prefix (``azure_ai/``, ``openai/``, ``vertex_ai/``,
    ``bedrock/``, ``ollama/``, ``mistral/``, ``cohere/``, ``groq/``) is
    treated as a literal and its pool absence is not an error. Everything
    else looks like a tag — if it isn't in the pool, surface the issue.
    """
    issues: list[str] = []
    pool = config.llm.model_pool.pool

    site_names = (
        "readiness",
        "ci_triage",
        "review_classification",
        "issue_triage",
        "cascade",
        "stuck_pr",
        "bot_identity",
        "dispatch_guard",
        "executor_routing",
        "crystallizer_category",
        "size_classifier",
    )

    def _is_literal(value: str) -> bool:
        if "/" in value:
            return True
        # Treat anything containing a hyphen-and-digit (model versioning)
        # as a likely literal; pure tags don't contain version digits.
        return bool(value) and any(c.isdigit() for c in value)

    for site_name in site_names:
        domain = getattr(config.agentic, site_name, None)
        if domain is None:
            continue
        consensus_cfg = getattr(domain, "consensus", None)
        if consensus_cfg is None:
            continue
        for label, value in [
            ("primary", consensus_cfg.primary),
            *[("escalation[%d]" % i, v) for i, v in enumerate(consensus_cfg.escalation)],
        ]:
            if value in pool:
                continue
            if _is_literal(value):
                continue
            issues.append(
                f"site {site_name!r} {label} references tag {value!r} "
                f"which is not present in llm.model_pool.pool"
            )

    return issues
```

Add the import at the top of `doctor.py` if needed:

```python
if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_doctor_consensus.py -v`
Expected: PASS — three doctor tests green.

- [ ] **Step 7: Commit**

```bash
git add src/caretaker/doctor.py tests/test_doctor_consensus.py caretaker_config.yaml.example
git commit -m "$(cat <<'EOF'
feat(doctor): validate consensus tags resolve in llm.model_pool

diagnose_consensus_config returns a list of human-readable issues;
empty list means valid. Distinguishes tags (must be in pool) from
literals (pass through unchanged). Example config gets the readiness +
size_classifier consensus blocks.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 18: End-to-end smoke test

**Files:**
- Create: `tests/test_consensus_e2e.py`

- [ ] **Step 1: Write the smoke test**

Create `tests/test_consensus_e2e.py`:

```python
"""End-to-end smoke: orchestrator init + readiness path uses the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from caretaker.config import (
    AgenticConfig,
    AgenticDomainConfig,
    ConsensusDomainConfig,
    LLMConfig,
    MaintainerConfig,
    ModelPoolConfig,
)
from caretaker.consensus import active as consensus_active
from caretaker.orchestrator import _build_consensus_engine


@dataclass
class _FakeClaude:
    responses: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def structured_complete(
        self, prompt: str, *, schema: type[Any], feature: str, system: str | None = None,
        model: str | None = None, max_retries: int | None = None, max_tokens: int = 2000,
    ) -> Any:
        self.calls.append(model or "<default>")
        queue = self.responses.get(model or "<default>", [])
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_e2e_orchestrator_engine_calls_two_distinct_models_for_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: build engine via orchestrator helper, swap claude, run readiness."""
    consensus_active.reset_for_tests()

    # Use real config types but with a fake ClaudeClient injected after
    # the engine is built.
    config = MaintainerConfig(
        llm=LLMConfig(
            model_pool=ModelPoolConfig(
                pool={
                    "reasoning_anthropic": "fake-anthropic",
                    "reasoning_alt": "fake-alt",
                },
            ),
        ),
        agentic=AgenticConfig(
            readiness=AgenticDomainConfig(
                mode="enforce",
                consensus=ConsensusDomainConfig(
                    strategy="always_two_models",
                    primary="reasoning_anthropic",
                    escalation=["reasoning_alt"],
                    agreement_fields=["verdict"],
                ),
            ),
        ),
    )

    engine = _build_consensus_engine(config)
    assert engine is not None

    # Inject a fake claude into the engine (bypassing real provider creds).
    from caretaker.pr_agent.readiness_llm import Readiness

    fake_claude = _FakeClaude(
        responses={
            "fake-anthropic": [
                Readiness(
                    verdict="ready",  # type: ignore[arg-type]
                    summary="ok",
                    confidence=0.9,
                    blockers=[],
                ),
            ],
            "fake-alt": [
                Readiness(
                    verdict="ready",  # type: ignore[arg-type]
                    summary="ok",
                    confidence=0.85,
                    blockers=[],
                ),
            ],
        },
    )
    engine._claude = fake_claude  # type: ignore[attr-defined]
    consensus_active.configure(engine)

    result = await engine.decide(
        site_name="readiness",
        schema=Readiness,
        system_prompt="sys",
        user_prompt="user",
        feature="readiness",
    )
    assert result.verdict.verdict == "ready"
    assert sorted(fake_claude.calls) == ["fake-alt", "fake-anthropic"]
    assert result.trace.escalated is False
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_consensus_e2e.py -v`
Expected: PASS.

- [ ] **Step 3: Run the entire test suite to confirm no regressions**

Run: `uv run pytest tests/ -v`
Expected: PASS — every test in the repo passes.

- [ ] **Step 4: Run linters**

Run: `uv run ruff check src/caretaker/consensus tests/test_consensus_*.py && uv run ruff format src/caretaker/consensus tests/test_consensus_*.py`

Expected: no errors; format is a no-op (or auto-fixes whitespace).

- [ ] **Step 5: Commit**

```bash
git add tests/test_consensus_e2e.py
git commit -m "$(cat <<'EOF'
test(consensus): e2e smoke for orchestrator-built engine

End-to-end smoke that builds the engine via the orchestrator helper,
injects a fake ClaudeClient, and confirms a two-model readiness
decision flows through both providers.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 19: Push branch and open implementation PR

**Files:**
- N/A — git operations only.

- [ ] **Step 1: Confirm branch state**

Run: `git status && git log --oneline -20`

Expected: clean working tree; ~18 commits since the design PR's first commit.

- [ ] **Step 2: Push the branch**

Run: `git push origin claude/zealous-shtern-4da9cc`

- [ ] **Step 3: Open the implementation PR**

The design PR is open at https://github.com/ianlintner/caretaker/pull/656. The implementation PR ships from the same branch, so this is a draft PR pointed at `main` that includes both design + implementation commits — or the design PR is upgraded from "design only" to "design + implementation."

Recommended: comment on PR #656 announcing implementation has been pushed, then mark PR #656 as ready for review (it now contains both the design and the implementation).

If a separate PR is preferred, branch `claude/consensus-engine-impl` from the current HEAD and use `gh pr create` with the body summarising the 18 implementation commits.

- [ ] **Step 4: Self-verify CI passes**

Watch the PR's checks. If CI fails, fix the failure in a new commit (do not amend) and push.

---

## Self-Review (run after writing the plan)

**1. Spec coverage:**

| Spec section | Task(s) |
|--------------|---------|
| `consensus/__init__.py` public surface | Task 1, 2, 6 |
| `consensus/trace.py` | Task 1 |
| `consensus/result.py` (ConsensusResult, ConsensusUnavailable) | Task 2 |
| `consensus/provider_pool.py` | Task 3 |
| `consensus/strategies.py` (TieredConfidence, AlwaysTwoModels) | Task 4, 5 |
| `consensus/engine.py` | Task 6 |
| `consensus/active.py` (process-wide holder) | Task 7 |
| `consensus/metrics.py` | Task 16 |
| `LLMConfig.model_pool` | Task 8 |
| `ConsensusDomainConfig` + validators | Task 9 |
| `AgenticDomainConfig.consensus` + `AgenticConfig.size_classifier` | Task 10 |
| `ShadowDecisionRecord.consensus_trace_json` | Task 11 |
| Wire engine into readiness | Task 12 |
| Wire engine into size_classifier | Task 13, 14 |
| Orchestrator builds engine | Task 15 |
| caretaker_config.yaml.example update + doctor validation | Task 17 |
| End-to-end smoke | Task 18 |

All spec sections covered.

**2. Placeholder scan:** No "TBD", "TODO", "implement later", or vague-error-handling phrases. Each step shows complete code. Check passes.

**3. Type consistency:**

- `StrategyContext` fields used in both `TieredConfidence` (Task 4) and `AlwaysTwoModels` (Task 5) — same names.
- `SiteConfig` from `engine.py` (Task 6) consumed in `_build_consensus_engine` (Task 15) — same field names.
- `ConsensusDomainConfig` from `config.py` (Task 9) read in `_build_consensus_engine` (Task 15) — `strategy`, `primary`, `escalation`, `confidence_threshold`, `agreement_fields` — all present in both.
- `decide_pre` / `decide_post` async signatures from Task 13 match the awaits in Task 14.
- `ConsensusUnavailable` raised in strategies (Task 4, 5) and caught in readiness wiring (Task 12) and size_classifier wiring (Task 13).

Check passes.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-04-30-llm-consensus-engine-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach?
