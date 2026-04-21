"""Graph builder — populates Neo4j from existing caretaker data.

Reads OrchestratorState, InsightStore, and agent registry to create
the graph nodes and relationships.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from caretaker.admin.causal_store import CausalEventStore  # noqa: TC001 (runtime-used)
from caretaker.agents._registry_data import AGENT_MODES, EVENT_AGENT_MAP
from caretaker.graph.models import NodeType, RelType
from caretaker.graph.store import GraphStore  # noqa: TC001 (runtime-used)
from caretaker.state.models import OrchestratorState  # noqa: TC001 (runtime-used)

logger = logging.getLogger(__name__)


def _bitemporal(
    valid_from: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return edge properties stamped with ``observed_at`` + bitemporal keys.

    M2 of the memory-graph plan: every edge grows ``observed_at`` (when
    caretaker recorded the fact), ``valid_from`` (when it became true),
    and ``valid_to`` (when it stopped, ``None`` == still current). The
    :class:`~caretaker.graph.writer.GraphWriter` fills ``observed_at``
    automatically; callers pass through ``valid_from`` when they know
    it. For the full-sync we synthesise ``valid_from = observed_at``
    because the sync can't know the true moment the fact became true.
    """
    now = datetime.now(UTC).isoformat()
    props: dict[str, Any] = {"observed_at": now, "valid_from": valid_from or now}
    if extra:
        props.update(extra)
    return props


# Which agents a given run *mode* dispatches. Mode values come from the
# RunSummary.mode field; unknown modes fall back to the full set so the
# graph never silently drops a run.
_MODE_TO_AGENTS: dict[str, tuple[str, ...]] = {
    "full": (
        "pr",
        "issue",
        "devops",
        "security",
        "deps",
        "docs",
        "charlie",
        "self-heal",
        "upgrade",
        "stale",
        "test",
        "release",
        "escalation",
        "review",
    ),
    "pr": ("pr",),
    "issue": ("issue",),
    "charlie": ("charlie",),
    "devops": ("devops",),
    "security": ("security",),
    "deps": ("deps",),
    "docs": ("docs",),
    "self-heal": ("self-heal",),
    "upgrade": ("upgrade",),
    "stale": ("stale",),
    "release": ("release",),
    "review": ("review",),
    "escalation": ("escalation",),
    "test": ("test",),
}


class GraphBuilder:
    """Populates the Neo4j graph from caretaker data sources."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    async def full_sync(
        self,
        state: OrchestratorState,
        insight_store: Any | None = None,
        causal_store: CausalEventStore | None = None,
    ) -> dict[str, int]:
        """Perform a full graph sync.  Returns counts of created entities."""
        counts: dict[str, int] = {
            "agents": 0,
            "prs": 0,
            "issues": 0,
            "goals": 0,
            "skills": 0,
            "runs": 0,
            "causal_events": 0,
            "edges": 0,
        }

        await self._store.ensure_indexes()

        # 1. Agents
        for agent_name, modes in AGENT_MODES.items():
            events = [e for e, agents in EVENT_AGENT_MAP.items() if agent_name in agents]
            await self._store.merge_node(
                NodeType.AGENT,
                f"agent:{agent_name}",
                {
                    "name": agent_name,
                    "modes": list(modes),
                    "events": events,
                },
            )
            counts["agents"] += 1

        # 2. Tracked PRs
        for number, pr in state.tracked_prs.items():
            pr_id = f"pr:{number}"
            await self._store.merge_node(
                NodeType.PR,
                pr_id,
                {
                    "number": number,
                    "state": pr.state,
                    "ownership_state": pr.ownership_state,
                    "readiness_score": pr.readiness_score,
                    "owned_by": pr.owned_by,
                    "ci_attempts": pr.ci_attempts,
                    "fix_cycles": pr.fix_cycles,
                    "escalated": pr.escalated,
                },
            )
            counts["prs"] += 1

            # PR agent relationships based on state
            if pr.ownership_state != "unowned":
                await self._store.merge_edge(
                    NodeType.AGENT,
                    "agent:pr",
                    NodeType.PR,
                    pr_id,
                    RelType.MONITORS,
                )
                counts["edges"] += 1

        # 3. Tracked Issues
        for number, issue in state.tracked_issues.items():
            issue_id = f"issue:{number}"
            await self._store.merge_node(
                NodeType.ISSUE,
                issue_id,
                {
                    "number": number,
                    "state": issue.state,
                    "classification": issue.classification,
                    "escalated": issue.escalated,
                },
            )
            counts["issues"] += 1

            # Issue ↔ PR linkage. Two directional edges landed in M2 so
            # "which PR resolves this issue" and "which issues does this
            # PR reference" are both one-hop queries — previously both
            # required walking the generic LINKED_TO rel.
            if issue.assigned_pr is not None:
                pr_id = f"pr:{issue.assigned_pr}"
                await self._store.merge_edge(
                    NodeType.PR,
                    pr_id,
                    NodeType.ISSUE,
                    issue_id,
                    RelType.REFERENCES,
                    _bitemporal(),
                )
                await self._store.merge_edge(
                    NodeType.ISSUE,
                    issue_id,
                    NodeType.PR,
                    pr_id,
                    RelType.RESOLVED_BY,
                    _bitemporal(),
                )
                counts["edges"] += 2

            # Agent triage relationship
            await self._store.merge_edge(
                NodeType.AGENT,
                "agent:issue",
                NodeType.ISSUE,
                issue_id,
                RelType.TRIAGES,
            )
            counts["edges"] += 1

        # 4. Goals. ``goal:overall`` is a synthetic aggregate that the
        # Run→Goal AFFECTED edge targets — ensuring it exists here so
        # the edge merge below can match both endpoints without
        # relying on per-run merge ordering.
        await self._store.merge_node(
            NodeType.GOAL,
            "goal:overall",
            {"name": "overall", "aggregate": True},
        )
        counts["goals"] += 1
        for goal_id, snapshots in state.goal_history.items():
            latest = snapshots[-1] if snapshots else None
            await self._store.merge_node(
                NodeType.GOAL,
                f"goal:{goal_id}",
                {
                    "name": goal_id,
                    "score": latest.score if latest else 0.0,
                    "status": latest.status if latest else "unknown",
                    "history_length": len(snapshots),
                },
            )
            counts["goals"] += 1

        # 5. Agent → Goal contributions (based on goal definitions)
        agent_goal_map: dict[str, list[str]] = {
            "ci_health": ["pr", "devops"],
            "pr_velocity": ["pr", "issue", "charlie", "stale"],
            "security_posture": ["security", "deps"],
            "self_health": ["self-heal", "upgrade"],
        }
        for goal_id, agent_names in agent_goal_map.items():
            for agent_name in agent_names:
                if f"goal:{goal_id}" in [f"goal:{g}" for g in state.goal_history]:
                    await self._store.merge_edge(
                        NodeType.AGENT,
                        f"agent:{agent_name}",
                        NodeType.GOAL,
                        f"goal:{goal_id}",
                        RelType.CONTRIBUTES_TO,
                    )
                    counts["edges"] += 1

        # 6. Run history
        for i, run in enumerate(state.run_history):
            # Prefer run_at-based id so the node survives across the
            # rolling 20-run window in state.run_history; falling back
            # to the index keeps the builder robust for fixtures that
            # omit timestamps.
            run_id = f"run:{run.run_at.isoformat()}" if run.run_at else f"run:{i}"
            run_at_iso = run.run_at.isoformat() if run.run_at else ""
            await self._store.merge_node(
                NodeType.RUN,
                run_id,
                {
                    "name": f"Run {i}",
                    "run_at": run_at_iso,
                    "mode": run.mode,
                    "prs_merged": run.prs_merged,
                    "issues_triaged": run.issues_triaged,
                    "goal_health": run.goal_health if run.goal_health is not None else 0.0,
                    "escalation_rate": run.escalation_rate,
                    "valid_from": run_at_iso,
                },
            )
            counts["runs"] += 1

            # Run → Agent EXECUTED edges (M2). The mapping is
            # mode-based: a "pr" run dispatches the PR agent, a "full"
            # run dispatches everything. valid_from marks the moment
            # the run started.
            agents_for_mode = _MODE_TO_AGENTS.get(run.mode, _MODE_TO_AGENTS["full"])
            for agent_name in agents_for_mode:
                await self._store.merge_edge(
                    NodeType.RUN,
                    run_id,
                    NodeType.AGENT,
                    f"agent:{agent_name}",
                    RelType.EXECUTED,
                    _bitemporal(valid_from=run_at_iso or None),
                )
                counts["edges"] += 1

            # Run → Goal AFFECTED with the run's contribution to
            # overall goal health. Matches the edge written by
            # StateTracker._emit_run_graph for live runs.
            if run.goal_health is not None:
                await self._store.merge_edge(
                    NodeType.RUN,
                    run_id,
                    NodeType.GOAL,
                    "goal:overall",
                    RelType.AFFECTED,
                    _bitemporal(
                        valid_from=run_at_iso or None,
                        extra={
                            "score": run.goal_health,
                            "escalation_rate": run.escalation_rate,
                        },
                    ),
                )
                counts["edges"] += 1

        # 7. Skills from InsightStore
        if insight_store is not None:
            for category in ("ci", "issue", "build", "security"):
                for skill in insight_store.top_skills(category, limit=9999):
                    await self._store.merge_node(
                        NodeType.SKILL,
                        f"skill:{skill.id}",
                        {
                            "name": skill.signature,
                            "category": skill.category,
                            "signature": skill.signature,
                            "confidence": skill.confidence,
                            "success_count": skill.success_count,
                            "fail_count": skill.fail_count,
                        },
                    )
                    counts["skills"] += 1

                    # Map skill category to agent
                    category_agent = {
                        "ci": "devops",
                        "issue": "issue",
                        "build": "devops",
                        "security": "security",
                    }
                    agent_name = category_agent.get(category, "pr")
                    await self._store.merge_edge(
                        NodeType.AGENT,
                        f"agent:{agent_name}",
                        NodeType.SKILL,
                        f"skill:{skill.id}",
                        RelType.LEARNED,
                    )
                    counts["edges"] += 1

        # 8. Causal events + CAUSED_BY edges
        if causal_store is not None:
            for event in causal_store.index().values():
                event_id = f"causal:{event.id}"
                await self._store.merge_node(
                    NodeType.CAUSAL_EVENT,
                    event_id,
                    {
                        "name": event.id,
                        "source": event.source,
                        "run_id": event.run_id or "",
                        "title": event.title,
                        "ref_kind": event.ref.kind,
                        "ref_number": event.ref.number if event.ref.number is not None else 0,
                        "observed_at": event.observed_at.isoformat() if event.observed_at else "",
                    },
                )
                counts["causal_events"] += 1

            # Second pass so both endpoints exist before edges are merged.
            for event in causal_store.index().values():
                if not event.parent_id:
                    continue
                if causal_store.get(event.parent_id) is None:
                    continue
                await self._store.merge_edge(
                    NodeType.CAUSAL_EVENT,
                    f"causal:{event.id}",
                    NodeType.CAUSAL_EVENT,
                    f"causal:{event.parent_id}",
                    RelType.CAUSED_BY,
                )
                counts["edges"] += 1

        logger.info(
            "Graph sync complete: %d agents, %d PRs, %d issues, %d goals, "
            "%d skills, %d runs, %d causal events, %d edges",
            counts["agents"],
            counts["prs"],
            counts["issues"],
            counts["goals"],
            counts["skills"],
            counts["runs"],
            counts["causal_events"],
            counts["edges"],
        )
        return counts
