"""Foundry — in-process coding executor as a Copilot alternative.

The executor reuses the workflow-runner's checked-out repository (via
``git worktree``), drives an LLM tool-use loop through an Azure AI Foundry
model (default; any LiteLLM-supported provider works), commits and pushes
the resulting diff with ``--force-with-lease``, and posts a result comment
that the existing PR state machine parses unchanged.

Exports:

- :class:`FoundryExecutor` — the end-to-end runner.
- :class:`CodingTask` / :class:`ExecutorResult` / :class:`ExecutorOutcome`.
- :class:`ExecutorDispatcher` — picks between Foundry and Copilot per task.
- :class:`ExecutorConfig` / :class:`FoundryExecutorConfig` — config models.
"""

from __future__ import annotations

from caretaker.foundry.config import ExecutorConfig, FoundryExecutorConfig
from caretaker.foundry.dispatcher import (
    ExecutorDispatcher,
    RouteOutcome,
    RouteResult,
)
from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    ExecutorResult,
    FoundryExecutor,
    TokenSupplier,
)

__all__ = [
    "CodingTask",
    "ExecutorConfig",
    "ExecutorDispatcher",
    "ExecutorOutcome",
    "ExecutorResult",
    "FoundryExecutor",
    "FoundryExecutorConfig",
    "RouteOutcome",
    "RouteResult",
    "TokenSupplier",
]
