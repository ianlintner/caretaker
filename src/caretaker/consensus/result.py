"""ConsensusResult and ConsensusUnavailable — the engine's return contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from caretaker.consensus.trace import ConsensusTrace, ModelAttempt

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ConsensusResult(Generic[T]):  # noqa: UP046 — PEP 695 syntax not yet supported by project mypy config
    """Successful engine output.

    ``verdict`` is the verdict the strategy chose to ship. ``trace`` is the
    full per-model audit record — always populated, even on the happy path
    where no escalation happened.
    """

    verdict: T
    trace: ConsensusTrace


class ConsensusUnavailable(RuntimeError):  # noqa: N818 — intentional public name, matches design doc
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
