"""Process-wide ContextVar for the current consensus decision's trace.

The :class:`~caretaker.consensus.ConsensusEngine` sets this ContextVar
before returning from :meth:`~caretaker.consensus.engine.ConsensusEngine.decide`.
The wrapping :func:`~caretaker.evolution.shadow.shadow_decision` decorator
reads it after the candidate function returns, and serialises the trace
onto :attr:`~caretaker.evolution.shadow.ShadowDecisionRecord.consensus_trace_json`.

ContextVars propagate up through ``await`` boundaries within the same
asyncio task, so a value set inside ``engine.decide()`` is visible to
the ``@shadow_decision`` wrapper after the candidate returns. Different
asyncio tasks have isolated contexts, so concurrent decisions on different
tasks do not see each other's traces.

Always reset the contextvar at the start of a fresh decision (the engine
does this implicitly by setting a new value on every successful
``decide()`` call). Tests can clear it explicitly via ``set(None)``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.consensus.trace import ConsensusTrace

current_trace_var: ContextVar[ConsensusTrace | None] = ContextVar(
    "caretaker_consensus_current_trace",
    default=None,
)


__all__ = ["current_trace_var"]
