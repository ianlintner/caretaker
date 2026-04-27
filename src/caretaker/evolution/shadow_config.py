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
    from caretaker.config import AgenticConfig, MaintainerConfig

_lock = threading.Lock()
_active: AgenticConfig | None = None
# Separate slot for the full :class:`MaintainerConfig` so the shadow
# decorator can reach ``llm.default_model`` without widening the
# ``AgenticConfig`` surface (``AgenticConfig`` intentionally has no
# backref to its parent — adding one would make the model graph
# cyclic, which pydantic v2 handles but which is confusing to read).
_active_maintainer: MaintainerConfig | None = None


def configure(config: AgenticConfig) -> None:
    """Install the active :class:`AgenticConfig`.

    Called from :class:`~caretaker.orchestrator.Orchestrator` startup
    and from the FastAPI lifespan hook in the admin backend. Idempotent.
    """
    global _active  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = config


def configure_maintainer(config: MaintainerConfig) -> None:
    """Install the full :class:`MaintainerConfig`.

    Sibling of :func:`configure`: the shadow decorator calls
    :func:`get_active_maintainer_config` to resolve
    ``llm.default_model`` for the ``candidate_model`` field on
    :class:`ShadowDecisionRecord`. Callers that already install the
    :class:`AgenticConfig` subfield via :func:`configure` can call this
    helper immediately after so the pair of slots stays consistent.
    """
    global _active, _active_maintainer  # noqa: PLW0603 — process singleton.
    with _lock:
        _active_maintainer = config
        _active = config.agentic


def get_active_config() -> AgenticConfig | None:
    """Return the installed config, or ``None`` when uncofigured.

    The decorator treats ``None`` as equivalent to every mode being
    ``off`` so import-order during tests does not matter.
    """
    with _lock:
        return _active


def get_active_maintainer_config() -> MaintainerConfig | None:
    """Return the installed :class:`MaintainerConfig`, if any.

    Returns ``None`` when only :func:`configure` has been called with a
    bare :class:`AgenticConfig` (the common test path) — the decorator
    treats that as "no known default_model" and stamps ``None`` on the
    ``candidate_model`` / ``legacy_model`` fields of the written
    :class:`ShadowDecisionRecord`.
    """
    with _lock:
        return _active_maintainer


def reset_for_tests() -> None:
    """Clear the active config. Used by test fixtures."""
    global _active, _active_maintainer  # noqa: PLW0603 — process singleton.
    with _lock:
        _active = None
        _active_maintainer = None


__all__ = [
    "configure",
    "configure_maintainer",
    "get_active_config",
    "get_active_maintainer_config",
    "reset_for_tests",
]
