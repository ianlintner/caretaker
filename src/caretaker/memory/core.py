"""Agent core-memory publisher — §4.3 of ``docs/memory-graph-plan.md``.

Every agent run publishes one small :class:`AgentCoreMemory` node into
the graph via the process-wide :class:`~caretaker.graph.writer.GraphWriter`.
The payload follows the Letta "core memory block" pattern — identity,
active goal, active run-id, active PR, a short ring of recent-action ids,
and an approximate context-token count — so the admin UI and future
agent-facing memory browser can answer "what was agent X thinking during
run R?" in a single cypher hop.

Design notes
------------

* :func:`publish` is non-blocking. It calls the writer singleton, which
  enqueues the node + edge and drains them on a background task. No
  SQLite or Neo4j IO happens on the dispatch hot path.
* Writes are upsert-only — the writer uses ``MERGE`` semantics, so a
  fresh ``publish`` call for the same ``(agent, run_id)`` tuple
  overwrites the in-place row. We deliberately never delete prior
  memory: the full audit lives in the per-agent SQLite KV store.
* The edge from :class:`AgentCoreMemory` to :class:`~caretaker.graph.models.NodeType.AGENT`
  uses the :data:`~caretaker.graph.models.RelType.CORE_MEMORY_OF`
  relationship so "which agent does this core-memory belong to?" is a
  direct one-hop query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer


@dataclass
class AgentCoreMemory:
    """Per-agent working-memory snapshot emitted once per run."""

    agent: str
    run_id: str
    repo: str
    identity: str
    active_goal: str | None = None
    active_pr: int | None = None
    recent_action_ids: list[str] = field(default_factory=list)
    context_tokens: int = 0


def publish(core: AgentCoreMemory) -> None:
    """Publish ``core`` to the graph via :func:`~caretaker.graph.writer.get_writer`.

    Enqueues one :data:`~caretaker.graph.models.NodeType.AGENT_CORE_MEMORY`
    node plus a :data:`~caretaker.graph.models.RelType.CORE_MEMORY_OF`
    edge to the owning ``:Agent{id=agent:<name>}`` node. Non-blocking,
    safe to call when the writer is disabled (e.g. unit tests without a
    Neo4j cluster) — every ``record_*`` call becomes a cheap no-op.

    The function never awaits and never raises; callers can wire it in
    at the start of each agent dispatch without worrying about hot-path
    latency or error propagation.
    """
    writer = get_writer()
    node_id = f"acm:{core.agent}:{core.run_id}"
    agent_id = f"agent:{core.agent}"

    node_props: dict[str, Any] = {
        "agent": core.agent,
        "run_id": core.run_id,
        "repo": core.repo,
        "identity": core.identity,
        "active_goal": core.active_goal,
        "active_pr": core.active_pr,
        # Flatten to a comma-separated string — Neo4j accepts primitive
        # arrays but the writer's retry loop is simpler if every prop is
        # a scalar. The ring rarely exceeds ~10 entries so the cost is
        # negligible.
        "recent_action_ids": ",".join(core.recent_action_ids),
        "context_tokens": core.context_tokens,
    }

    writer.record_node(NodeType.AGENT_CORE_MEMORY.value, node_id, node_props)
    writer.record_edge(
        source_label=NodeType.AGENT_CORE_MEMORY.value,
        source_id=node_id,
        target_label=NodeType.AGENT.value,
        target_id=agent_id,
        rel_type=RelType.CORE_MEMORY_OF.value,
        properties={"repo": core.repo},
    )


__all__ = ["AgentCoreMemory", "publish"]
