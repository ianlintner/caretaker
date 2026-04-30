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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from caretaker.consensus.result import ConsensusResult, ConsensusUnavailable
from caretaker.consensus.trace import ConsensusTrace, ModelAttempt
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic import BaseModel

    from caretaker.consensus.provider_pool import ProviderPool
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="BaseModel")


@dataclass(frozen=True)
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
    claude: ClaudeClient
    max_tokens: int = 2000


class Strategy(Protocol):
    """Strategy protocol — every concrete strategy implements ``run``."""

    name: str

    async def run(self, ctx: StrategyContext) -> ConsensusResult[Any]: ...


# ── Helpers ───────────────────────────────────────────────────────────────


def _verdict_summary(verdict: Any) -> str:
    """Short, JSON-safe summary of a verdict for the audit trail."""
    for field_name in ("verdict", "label", "decision", "category", "stuck_reason"):
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
        # Primary appears first so it wins ties — Python's ``max`` returns
        # the first maximum, which matches operator intuition (ship the
        # cheaper default unless escalation is strictly stronger).
        candidate_pool: list[tuple[Any, ModelAttempt]] = []
        if primary_verdict is not None:
            candidate_pool.append((primary_verdict, primary_attempt))
        candidate_pool.extend(escalation_verdicts)

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

        # Tiebreaker exists — it's the verdict we ship, regardless of
        # whether it agreed with a prior or cast a fresh deciding vote.
        # ``final_model`` always points at the model that produced the
        # shipped verdict so cost/latency/bug triage stays unambiguous.
        # Agreement information remains reconstructable from ``attempts``.
        if tiebreaker_verdict is not None and tiebreaker_attempt is not None:
            return ConsensusResult(
                verdict=tiebreaker_verdict,
                trace=ConsensusTrace(
                    strategy=self.name,
                    attempts=attempts,
                    escalated=True,
                    final_model=tiebreaker_attempt.model,
                ),
            )

        # Tiebreaker tier exhausted (or absent) AND we lack two-model agreement.
        # The strategy's contract is "two models must agree (or a tiebreaker
        # breaks the tie)" — silently shipping the higher-confidence verdict
        # would contradict that. Raise ConsensusUnavailable so the wrapping
        # @shadow_decision falls through to the legacy heuristic, which on
        # readiness returns "block-merge" (the safe default).
        raise ConsensusUnavailable(
            strategy=self.name,
            attempts=attempts,
            reason=(
                "two-model disagreement and tiebreaker tier exhausted; "
                "consensus contract requires agreement or a casting vote"
            ),
        )


__all__ = [
    "AlwaysTwoModels",
    "Strategy",
    "StrategyContext",
    "TieredConfidence",
]
