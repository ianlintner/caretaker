"""Agent protocol — shared types and base class for all caretaker agents."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.llm.router import LLMRouter
    from caretaker.state.memory import MemoryStore
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """Shared context passed to every agent at construction time."""

    github: GitHubClient
    owner: str
    repo: str
    config: MaintainerConfig
    llm_router: LLMRouter
    dry_run: bool = False
    memory: MemoryStore | None = None
    mcp_client: Any | None = None
    telemetry: Any | None = None


@dataclass
class AgentResult:
    """Standardised result envelope returned by every agent."""

    processed: int = 0
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    """Abstract base class that every caretaker agent must implement."""

    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx

    # ── Identity ──────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Short, unique agent name used in logging and the registry."""

    # ── Lifecycle ─────────────────────────────────────────────

    @abstractmethod
    def enabled(self) -> bool:
        """Return True when the agent's config section says it is active."""

    @abstractmethod
    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Run the agent's core logic.

        Args:
            state: The persisted orchestrator state (may be mutated).
            event_payload: Optional GitHub webhook payload for event-driven runs.

        Returns:
            An ``AgentResult`` summarising what happened.
        """

    @abstractmethod
    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        """Map this agent's result fields onto the shared ``RunSummary``.

        This keeps summary-field knowledge co-located with the agent that
        produces it, rather than in the orchestrator.
        """
