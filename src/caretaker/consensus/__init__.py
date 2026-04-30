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
