"""Concrete AgentRunner for GitHub App webhook dispatch.

Delegates to the central AgentRegistry so the dispatcher never needs
to import agent adapters or know about OrchestratorState.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.agents._registry_data import build_registry
from caretaker.state.models import OrchestratorState, RunSummary
from caretaker.state.tracker import StateTracker

if TYPE_CHECKING:
    from caretaker.agent_protocol import AgentContext
    from caretaker.github_app.webhooks import ParsedWebhook

logger = logging.getLogger(__name__)


class RegistryAgentRunner:
    """Run a named agent via the caretaker :class:`~caretaker.registry.AgentRegistry`.

    One instance is shared across all deliveries.  Each :meth:`run` call
    loads persisted :class:`~caretaker.state.models.OrchestratorState` from
    the repo's tracking issue so counters like ``ci_attempts`` survive across
    multiple webhook deliveries for the same PR.  State is saved back after
    the agent completes.

    If state load fails (network error, no tracking issue yet) the run
    proceeds with fresh state and a warning is logged — the same behaviour
    as before this change.

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

        tracker = StateTracker(context.github, context.owner, context.repo)
        try:
            state = await tracker.load()
        except Exception:
            logger.warning(
                "webhook runner: failed to load state for %s/%s (delivery=%s); using fresh state",
                context.owner,
                context.repo,
                parsed.delivery_id,
                exc_info=True,
            )
            state = OrchestratorState()

        summary = RunSummary()

        result = await registry.run_one(
            agent,
            state,
            summary,
            event_payload=parsed.payload,
        )

        try:
            await tracker.save()
        except Exception:
            logger.warning(
                "webhook runner: failed to save state for %s/%s (delivery=%s)",
                context.owner,
                context.repo,
                parsed.delivery_id,
                exc_info=True,
            )

        if result is None or result.errors:
            return "failure"
        return "success"


__all__ = ["RegistryAgentRunner"]
