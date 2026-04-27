"""Protocol every BYOCA coding agent implements.

The dispatcher routes :class:`~caretaker.foundry.executor.CodingTask`
items at agents through this contract; nothing in
:class:`~caretaker.foundry.dispatcher.ExecutorDispatcher` knows about
the underlying provider (Claude Code, opencode, foundry, …).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from caretaker.foundry.executor import CodingTask, ExecutorResult
    from caretaker.github_client.models import PullRequest

# ``handoff`` is the only mode wired up in Phase 1. ``inline`` is what the
# foundry executor uses today (and is what future opencode/codex/gemini
# subprocess agents will use). ``k8s_job`` is reserved for the planned
# k8s-job execution path (``infra/k8s/caretaker-agent-worker.yaml``).
ExecutionMode = Literal["handoff", "inline", "k8s_job"]


@runtime_checkable
class CodingAgent(Protocol):
    """Minimal contract for a registry-backed coding agent.

    Implementations must expose the four attributes below and an async
    :meth:`run` method matching the existing executor shape so the
    dispatcher can fall back to Copilot on ``ESCALATED`` / ``FAILED``
    without special-casing the agent name.
    """

    name: str  # registry key — matches ``executor.provider`` and ``agent:<name>`` labels.
    mode: ExecutionMode

    @property
    def enabled(self) -> bool: ...

    async def run(self, task: CodingTask, pr: PullRequest) -> ExecutorResult: ...


__all__ = ["CodingAgent", "ExecutionMode"]
