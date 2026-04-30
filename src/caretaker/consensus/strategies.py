"""Pluggable consensus strategies.

Each strategy implements the same async ``run(ctx) -> ConsensusResult``
contract, raising :class:`ConsensusUnavailable` when every model attempt
errored out. The engine selects a strategy by name from
``ConsensusDomainConfig.strategy``.
"""

from __future__ import annotations

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
    claude: ClaudeClient
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
