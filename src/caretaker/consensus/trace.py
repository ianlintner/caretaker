"""Per-decision consensus audit records.

A :class:`ConsensusTrace` is the authoritative serialised audit for one
``engine.decide(...)`` call. It records every model attempt — including
errors — in attempt order, plus the final model whose verdict was shipped.

The schema is JSON-serialised onto
:attr:`ShadowDecisionRecord.consensus_trace_json` for persistence and
surfaced through the existing admin API. The wire-up uses a ContextVar
(:data:`caretaker.consensus.trace_context.current_trace_var`) that
``engine.decide`` sets on success and ``@shadow_decision`` reads when
writing the decision record.

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
            "Short summary of the model's verdict for log readability. ``None`` on error."
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
