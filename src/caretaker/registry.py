"""Agent registry — discovers, stores, and dispatches BaseAgent subclasses."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.observability import agent_span

if TYPE_CHECKING:
    from caretaker.agent_protocol import AgentResult, BaseAgent
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Central catalogue of all registered agents.

    Usage::

        registry = AgentRegistry()
        registry.register(PRAgentV2(ctx))
        registry.register(IssueAgentV2(ctx))
        ...
        await registry.run_all(state, summary, mode="full")
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._mode_map: dict[str, set[str]] = {}

    # ── Registration ───────────────────────────────────────────

    def register(self, agent: BaseAgent, modes: set[str] | None = None) -> None:
        """Register *agent* and the run-modes that should include it.

        Args:
            agent: A ``BaseAgent`` instance.
            modes: Set of mode strings (e.g. ``{"full", "pr-only"}``) under
                which this agent should run.
        """
        name = agent.name
        if name in self._agents:
            raise ValueError(f"Agent '{name}' is already registered")
        self._agents[name] = agent
        effective_modes = modes if modes is not None else set()
        self._mode_map[name] = effective_modes
        logger.debug("Registered agent '%s' for modes %s", name, effective_modes)

    def get(self, name: str) -> BaseAgent | None:
        return self._agents.get(name)

    @property
    def agents(self) -> dict[str, BaseAgent]:
        return dict(self._agents)

    # ── Dispatch ───────────────────────────────────────────────

    def agents_for_mode(self, mode: str) -> list[BaseAgent]:
        """Return the ordered list of agents that should run for *mode*."""
        return [
            agent for name, agent in self._agents.items() if mode in self._mode_map.get(name, set())
        ]

    async def run_all(
        self,
        state: OrchestratorState,
        summary: RunSummary,
        mode: str = "full",
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """Execute every agent that matches *mode*, in registration order."""
        for agent in self.agents_for_mode(mode):
            await self.run_one(agent, state, summary, event_payload=event_payload)

    async def run_one(
        self,
        agent: BaseAgent,
        state: OrchestratorState,
        summary: RunSummary,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult | None:
        """Execute a single agent with full lifecycle guards."""
        if not agent.enabled():
            logger.info("%s agent is disabled", agent.name)
            return None

        if agent._ctx.dry_run:
            logger.info("[DRY RUN] %s agent would run", agent.name)
            return None

        # M8 of the memory-graph plan — emit one ``invoke_agent`` OTel
        # GenAI span per agent dispatch. No-op when the OTel SDK is not
        # installed or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset. The
        # ``caretaker.run_id`` attribute mirrors the run_at timestamp so
        # spans can be joined against ``RunSummary`` / graph ``Run`` nodes.
        run_id = summary.run_at.isoformat() if summary.run_at else ""
        with agent_span(agent_name=agent.name, operation="run") as span:
            span.set_attribute("caretaker.run_id", run_id)
            try:
                result = await agent.execute(state, event_payload=event_payload)
                agent.apply_summary(result, summary)
                summary.errors.extend(result.errors)
                return result
            except Exception as exc:
                logger.error("%s agent error: %s", agent.name, exc, exc_info=True)
                summary.errors.append(f"{agent.name}: {exc}")
                return None
