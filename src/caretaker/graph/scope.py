"""Active graph scope — propagates the current PR/Issue context.

Agents touch memory through the protocol-typed :class:`MemoryBackend`, so the
``set`` call site can't carry an explicit "this write belongs to PR #123"
parameter without rippling through every backend. This module provides a
``contextvars`` channel instead: callers entering a PR/Issue-scoped block
push a :class:`Scope`, and the Neo4j memory backend (and any future
graph-aware writer) reads the active value at write time to stamp
``TOUCHED_MEMORY`` edges.

The contextvar is async-safe — each ``asyncio.Task`` gets its own snapshot,
so concurrent agent runs scoped to different PRs do not bleed into one
another. When no scope is active, ``current()`` returns ``None`` and the
graph write is silently skipped.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class Scope:
    """The PR/Issue context currently driving an agent run.

    ``repo`` is the ``owner/name`` slug (used when stamping ``BELONGS_TO``
    on auxiliary nodes). ``pr_number`` and ``issue_number`` are mutually
    permissible — set whichever the inbound webhook references; both may
    be set when a PR closes/references an issue.
    """

    repo: str = ""
    pr_number: int | None = None
    issue_number: int | None = None


_active: ContextVar[Scope | None] = ContextVar("caretaker_graph_scope", default=None)


def current() -> Scope | None:
    """Return the active scope, or ``None`` when no agent context is set."""
    return _active.get()


@contextlib.contextmanager
def active_scope(scope: Scope) -> Iterator[None]:
    """Push *scope* onto the contextvar stack for the duration of a block.

    Use as ``with active_scope(Scope(repo=..., pr_number=...)):`` around any
    code path that should attribute its memory writes to a specific entity.
    Restores the previous value on exit even if the block raises.
    """
    token = _active.set(scope)
    try:
        yield
    finally:
        _active.reset(token)


__all__ = ["Scope", "active_scope", "current"]
