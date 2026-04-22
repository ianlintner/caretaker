"""Process-wide :class:`AgenticConfig` resolver for the shadow decorator.

The decorator in :mod:`caretaker.evolution.shadow` needs to know the
current mode (``off`` / ``shadow`` / ``enforce``) per decision name at
call time, but it must not pull a MaintainerConfig instance through
every wrapped signature — call sites are existing heuristics and
changing each signature would be invasive.

The pattern mirrors :class:`~caretaker.graph.writer.GraphWriter`:
the orchestrator / admin backend calls :func:`configure` once at
startup with the loaded config, and the decorator reads from the
module-level cache. Tests use :func:`configure` and
:func:`reset_for_tests` to set and clear the override without side-
effects.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import AgenticConfig

_lock = threading.Lock()
_active: AgenticConfig | None = None


def configure(config: AgenticConfig) -> None:
    """Install the active :class:`AgenticConfig`.

    Called from :class:`~caretaker.orchestrator.Orchestrator` startup
    and from the FastAPI lifespan hook in the admin backend. Idempotent.
    """
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = config


def get_active_config() -> AgenticConfig | None:
    """Return the installed config, or ``None`` when uncofigured.

    The decorator treats ``None`` as equivalent to every mode being
    ``off`` so import-order during tests does not matter.
    """
    with _lock:
        return _active


def reset_for_tests() -> None:
    """Clear the active config. Used by test fixtures."""
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = None


__all__ = ["configure", "get_active_config", "reset_for_tests"]
