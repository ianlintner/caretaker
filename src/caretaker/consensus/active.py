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
_active: ConsensusEngine | None = None


def configure(engine: ConsensusEngine) -> None:
    """Install the active engine. Idempotent."""
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = engine


def get_active_engine() -> ConsensusEngine | None:
    """Return the installed engine, or ``None`` when unconfigured."""
    with _lock:
        return _active


def reset_for_tests() -> None:
    """Clear the active engine. Used by test fixtures."""
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = None


__all__ = ["configure", "get_active_engine", "reset_for_tests"]
