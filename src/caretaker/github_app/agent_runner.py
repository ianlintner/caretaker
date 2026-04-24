"""Concrete AgentRunner for GitHub App webhook dispatch.

Delegates to the central AgentRegistry so the dispatcher never needs
to import agent adapters or know about OrchestratorState.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.agents._registry_data import build_registry
from caretaker.state.models import OrchestratorState, RunSummary

if TYPE_CHECKING:
    from caretaker.agent_protocol import AgentContext
    from caretaker.github_app.webhooks import ParsedWebhook

logger = logging.getLogger(__name__)


class RegistryAgentRunner:
    """Run a named agent via the caretaker :class:`~caretaker.registry.AgentRegistry`.

    One instance is shared across all deliveries.  Each :meth:`run` call
    builds a fresh registry (and fresh ephemeral state) from the per-delivery
    :class:`~caretaker.agent_protocol.AgentContext`, so there is no cross-
    delivery state leakage.

    Returns a bounded outcome string:
    - ``"success"`` — agent ran without errors.
    - ``"failure"`` — agent raised or returned errors.
    - ``"disabled"`` — agent not found or reports ``enabled() == False``.
    """

    async def run(
        self,
        *,
        agent_name: str,
        context: AgentContext,
        parsed: ParsedWebhook,
    ) -> str:
        registry = build_registry(context)
        agent = registry.get(agent_name)

        if agent is None:
            logger.warning(
                "webhook runner: agent %r not found in registry delivery=%s",
                agent_name,
                parsed.delivery_id,
            )
            return "disabled"

        if not agent.enabled():
            logger.info(
                "webhook runner: agent %r is disabled delivery=%s",
                agent_name,
                parsed.delivery_id,
            )
            return "disabled"

        state = OrchestratorState()
        summary = RunSummary()

        result = await registry.run_one(
            agent,
            state,
            summary,
            event_payload=parsed.payload,
        )

        if result is None or result.errors:
            return "failure"
        return "success"


__all__ = ["RegistryAgentRunner"]
