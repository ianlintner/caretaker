"""Graph builder — populates Neo4j from existing caretaker data.

Reads OrchestratorState, InsightStore, and agent registry to create
the graph nodes and relationships.
"""

from __future__ import annotations

import logging
from typing import Any

from caretaker.agents._registry_data import AGENT_MODES, EVENT_AGENT_MAP
from caretaker.graph.models import NodeType, RelType
from caretaker.graph.store import GraphStore  # noqa: TC001 (runtime-used)
from caretaker.state.models import OrchestratorState  # noqa: TC001 (runtime-used)

logger = logging.getLogger(__name__)


class GraphBuilder:
    """Populates the Neo4j graph from caretaker data sources."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    async def full_sync(
        self,
        state: OrchestratorState,
        insight_store: Any | None = None,
    ) -> dict[str, int]:
        """Perform a full graph sync.  Returns counts of created entities."""
        counts: dict[str, int] = {
            "agents": 0,
            "prs": 0,
            "issues": 0,
            "goals": 0,
            "skills": 0,
            "runs": 0,
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

            # Issue → PR linkage
            if issue.assigned_pr is not None:
                await self._store.merge_edge(
                    NodeType.PR,
                    f"pr:{issue.assigned_pr}",
                    NodeType.ISSUE,
                    issue_id,
                    RelType.LINKED_TO,
                )
                counts["edges"] += 1

            # Agent triage relationship
            await self._store.merge_edge(
                NodeType.AGENT,
                "agent:issue",
                NodeType.ISSUE,
                issue_id,
                RelType.TRIAGES,
            )
            counts["edges"] += 1

        # 4. Goals
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
            run_id = f"run:{i}"
            await self._store.merge_node(
                NodeType.RUN,
                run_id,
                {
                    "name": f"Run {i}",
                    "run_at": run.run_at.isoformat() if run.run_at else "",
                    "mode": run.mode,
                    "prs_merged": run.prs_merged,
                    "issues_triaged": run.issues_triaged,
                    "goal_health": run.goal_health,
                    "escalation_rate": run.escalation_rate,
                },
            )
            counts["runs"] += 1

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

        logger.info(
            "Graph sync complete: %d agents, %d PRs, %d issues, %d goals, "
            "%d skills, %d runs, %d edges",
            counts["agents"],
            counts["prs"],
            counts["issues"],
            counts["goals"],
            counts["skills"],
            counts["runs"],
            counts["edges"],
        )
        return counts
