"""BYOCA coding-agent registry.

The registry holds the set of pluggable coding agents available to the
:class:`~caretaker.foundry.dispatcher.ExecutorDispatcher`. Phase 1 ships
the ``claude_code`` and ``opencode`` hand-off agents and a placeholder
slot for ``foundry`` (caretaker's own inline tool-loop). Future phases
plug in additional agents — codex, gemini, hermes — through the same
:meth:`register` call.

The registry intentionally does NOT instantiate agents itself. Caretaker
constructs them in ``Orchestrator._build_executor_dispatcher`` so the
GitHub client, owner/repo, and per-agent config are wired in from one
place. The registry just stores them and exposes lookup helpers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.coding_agents.protocol import CodingAgent

logger = logging.getLogger(__name__)


class CodingAgentRegistry:
    """In-memory registry of named :class:`CodingAgent` instances."""

    def __init__(self) -> None:
        self._agents: dict[str, CodingAgent] = {}

    def register(self, agent: CodingAgent) -> None:
        """Register an agent under its ``name``.

        Re-registering the same name overwrites the previous entry; this
        is convenient for tests but should be a warning sign in
        production. We log at WARNING so it shows up.
        """
        if agent.name in self._agents:
            logger.warning("CodingAgentRegistry: overwriting existing agent %r", agent.name)
        self._agents[agent.name] = agent

    def get(self, name: str) -> CodingAgent | None:
        """Return the agent registered under ``name``, or ``None``."""
        return self._agents.get(name)

    def has(self, name: str) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        """All registered agent names, in insertion order."""
        return list(self._agents)

    def enabled(self) -> list[CodingAgent]:
        """Currently-enabled agents, in registration order."""
        return [a for a in self._agents.values() if a.enabled]

    def fallback_chain(self, primary: str) -> list[CodingAgent]:
        """Order to try when the primary agent isn't available.

        Returns ``primary`` first (if registered + enabled), then any
        other enabled custom agents in registration order. Copilot is
        not represented here — it lives outside the registry as the
        terminal fallback owned by :class:`ExecutorDispatcher`.
        """
        chain: list[CodingAgent] = []
        primary_agent = self._agents.get(primary)
        if primary_agent is not None and primary_agent.enabled:
            chain.append(primary_agent)
        for name, agent in self._agents.items():
            if name == primary:
                continue
            if agent.enabled:
                chain.append(agent)
        return chain


__all__ = ["CodingAgentRegistry"]
