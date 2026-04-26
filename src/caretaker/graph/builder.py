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
from caretaker.causal_chain import CausalEvent, parse_run_id
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


# M3: ``owned_by`` values in :class:`TrackedPR` are free-form strings, but
# in the wild they collapse to one of four constants: ``copilot``,
# ``foundry``, ``claude_code`` (external executors), and ``caretaker``
# (the orchestrator itself — i.e. no delegation). Only the first three
# are modelled as :class:`NodeType.EXECUTOR` nodes with a ``HANDLED_BY``
# edge; ``caretaker`` and any unknown value is treated as "no executor"
# so we don't pollute the graph with spurious self-loops.
_EXECUTOR_PROVIDERS: frozenset[str] = frozenset({"copilot", "foundry", "claude_code"})


class GraphBuilder:
    """Populates the Neo4j graph from caretaker data sources."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    async def full_sync(
        self,
        state: OrchestratorState,
        insight_store: Any | None = None,
        causal_store: CausalEventStore | None = None,
        repo: str | None = None,
    ) -> dict[str, int]:
        """Perform a full graph sync.  Returns counts of created entities.

        ``repo`` is the ``owner/name`` slug for the tenant being synced.
        When provided, a dedicated ``:Repo`` node is merged and every
        other node gains a ``repo`` scalar so cypher queries can scope
        by tenant (M3 of the memory-graph plan). Defaults to
        ``"unknown/unknown"`` so the builder stays callable from legacy
        call sites that have not yet been threaded with the slug.
        """
        counts: dict[str, int] = {
            "agents": 0,
            "prs": 0,
            "issues": 0,
            "goals": 0,
            "skills": 0,
            "runs": 0,
            "causal_events": 0,
            "executors": 0,
            "comments": 0,
            "edges": 0,
        }

        await self._store.ensure_indexes()

        async def _belongs_to(label: NodeType, node_id: str) -> None:
            """Anchor a per-tenant node to its owning :Repo node.

            Centralised so every section of full_sync uses identical
            edge properties; the bitemporal stamp keeps temporal
            queries ("which PRs belonged to repo X on date D") cheap.
            """
            await self._store.merge_edge(
                label,
                node_id,
                NodeType.REPO,
                repo_id,
                RelType.BELONGS_TO,
                _bitemporal(),
            )
            counts["edges"] += 1

        # 0. Tenant Repo node. Every other node merged below carries
        # ``repo=<slug>`` so the whole per-tenant subgraph is scopable.
        # Callers that haven't yet been plumbed with the slug get the
        # ``unknown/unknown`` placeholder — easier to detect in prod
        # than silently dropping the scope property.
        repo_slug = repo or "unknown/unknown"
        repo_id = f"repo:{repo_slug}"
        await self._store.merge_node(
            NodeType.REPO,
            repo_id,
            {"slug": repo_slug, "repo": repo_slug},
        )

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
                    "repo": repo_slug,
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
                    "repo": repo_slug,
                },
            )
            counts["prs"] += 1
            await _belongs_to(NodeType.PR, pr_id)

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

            # M3: PR → Executor attribution. ``owned_by`` narrows to one
            # of the known providers before we merge an :Executor node;
            # ``caretaker`` (self) and unknown values skip the edge so
            # queries like "which executor fixed this PR" don't have to
            # filter out the default no-delegation case.
            if pr.owned_by in _EXECUTOR_PROVIDERS:
                executor_id = f"executor:{pr.owned_by}"
                await self._store.merge_node(
                    NodeType.EXECUTOR,
                    executor_id,
                    {
                        "provider": pr.owned_by,
                        "repo": repo_slug,
                    },
                )
                counts["executors"] += 1
                await _belongs_to(NodeType.EXECUTOR, executor_id)
                # Use ownership_acquired_at when known so ``valid_from``
                # marks when this executor actually took the PR, not
                # when the sync happened to notice.
                acquired = (
                    pr.ownership_acquired_at.isoformat() if pr.ownership_acquired_at else None
                )
                await self._store.merge_edge(
                    NodeType.PR,
                    pr_id,
                    NodeType.EXECUTOR,
                    executor_id,
                    RelType.HANDLED_BY,
                    _bitemporal(valid_from=acquired),
                )
                counts["edges"] += 1

            # NOTE: ``:CheckRun`` node emission (name / conclusion /
            # run_id / PR→CheckRun edge) lives with the live event
            # feed planned for M5 — :class:`TrackedPR` tracks
            # ``ci_attempts`` as a counter but doesn't carry the
            # per-check metadata the schema calls for, so synthesising
            # one here would mean inventing data. Skipped
            # intentionally; the constraint + NodeType are shipped
            # now so the M5 writer has something to merge into.

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
                    "repo": repo_slug,
                },
            )
            counts["issues"] += 1
            await _belongs_to(NodeType.ISSUE, issue_id)

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
            {"name": "overall", "aggregate": True, "repo": repo_slug},
        )
        counts["goals"] += 1
        await _belongs_to(NodeType.GOAL, "goal:overall")
        for goal_id, snapshots in state.goal_history.items():
            latest = snapshots[-1] if snapshots else None
            full_goal_id = f"goal:{goal_id}"
            await self._store.merge_node(
                NodeType.GOAL,
                full_goal_id,
                {
                    "name": goal_id,
                    "score": latest.score if latest else 0.0,
                    "status": latest.status if latest else "unknown",
                    "history_length": len(snapshots),
                    "repo": repo_slug,
                },
            )
            counts["goals"] += 1
            await _belongs_to(NodeType.GOAL, full_goal_id)

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
                    "repo": repo_slug,
                },
            )
            counts["runs"] += 1
            await _belongs_to(NodeType.RUN, run_id)

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
                    skill_id = f"skill:{skill.id}"
                    await self._store.merge_node(
                        NodeType.SKILL,
                        skill_id,
                        {
                            "name": skill.signature,
                            "category": skill.category,
                            "signature": skill.signature,
                            "confidence": skill.confidence,
                            "success_count": skill.success_count,
                            "fail_count": skill.fail_count,
                            "repo": repo_slug,
                        },
                    )
                    counts["skills"] += 1
                    await _belongs_to(NodeType.SKILL, skill_id)

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

        # 8. Causal events + materialised relationships. The previous
        # implementation merged each :CausalEvent as an island — only
        # CAUSED_BY chains linked them to anything else, leaving
        # thousands of orphan nodes in the graph (the symptom that
        # opened this work item). This pass lifts every scalar
        # reference into a real edge:
        #
        #   * ``ref_kind`` / ``ref_number`` → ``(:CausalEvent)-[:ON]->(:PR|:Issue|:Comment)``
        #   * ``ref_kind == "comment"``     → materialise a ``:Comment`` node,
        #                                     attach it to its parent
        #                                     PR/Issue (also via ``ON``),
        #                                     and emit
        #                                     ``(:Comment)-[:EMITS]->(:CausalEvent)``
        #                                     so the comment carries
        #                                     the marker.
        #   * ``run_id`` (parsed from the   → materialise a *live*
        #     causal id)                      ``:Run`` keyed by the
        #                                     GitHub workflow id and
        #                                     attach
        #                                     ``(:Run)-[:HAS_EVENT]->(:CausalEvent)``.
        #
        # The state-tracker-emitted ``:Run`` nodes (id =
        # ``run:<isoformat>``) describe the orchestrator's view of a
        # cycle but cannot be joined directly to causal events because
        # ``RunSummary`` doesn't carry the workflow id. The two
        # populations are intentionally kept separate; queries that
        # need both can hop through ``BELONGS_TO`` on the shared
        # :Repo node and filter by ``run_at`` window.
        # M8 of the memory-graph plan adds ``span_id`` +
        # ``parent_span_id`` properties when the CausalEvent was
        # captured inside an OTel ``invoke_agent`` span — makes
        # "which span caused this escalation" a one-hop cypher query
        # that can be joined against the trace backend.
        if causal_store is not None:
            # Track which (number, comment_id) tuples we've already
            # materialised so a comment that's referenced by multiple
            # markers (rare today, common after the multi-marker fix)
            # only gets one :Comment node.
            comment_seen: set[str] = set()
            # Track which live workflow run ids we've already merged so
            # one Run node represents many CausalEvents from the same
            # workflow run.
            live_run_seen: set[str] = set()

            causal_events: list[CausalEvent] = list(causal_store.index().values())

            # Pass 1: merge :CausalEvent + :Comment + live :Run nodes
            # so every endpoint exists before pass 2 wires edges.
            for event in causal_events:
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
                        "repo": repo_slug,
                        "span_id": event.span_id or "",
                        "parent_span_id": event.parent_span_id or "",
                        # parent_id is stamped on the node so the SPA
                        # can filter on it without traversing edges.
                        "parent_id": event.parent_id or "",
                    },
                )
                counts["causal_events"] += 1
                await _belongs_to(NodeType.CAUSAL_EVENT, event_id)

                # Materialise a :Comment node when the marker came
                # from a GitHub comment (vs an issue/PR body). The
                # node id encodes both the parent thread number and
                # the comment id so it's stable across refreshes.
                if (
                    event.ref.kind == "comment"
                    and event.ref.number is not None
                    and event.ref.comment_id is not None
                ):
                    comment_id = f"comment:{event.ref.number}:{event.ref.comment_id}"
                    if comment_id not in comment_seen:
                        comment_seen.add(comment_id)
                        await self._store.merge_node(
                            NodeType.COMMENT,
                            comment_id,
                            {
                                "thread_number": event.ref.number,
                                "comment_id": event.ref.comment_id,
                                "observed_at": event.observed_at.isoformat()
                                if event.observed_at
                                else "",
                                "repo": repo_slug,
                            },
                        )
                        counts["comments"] += 1
                        await _belongs_to(NodeType.COMMENT, comment_id)

                # Materialise a live :Run keyed by the GitHub
                # workflow id parsed off the causal id (e.g.
                # ``run-24944914320-...`` → ``run:gh:24944914320``).
                # Distinct from the state-tracker run id so we don't
                # collide on the unique constraint, and so live runs
                # remain visible after the rolling 20-run state
                # window evicts the orchestrator-side row.
                live_run_id = parse_run_id(event.id)
                if live_run_id and live_run_id not in live_run_seen:
                    live_run_seen.add(live_run_id)
                    live_run_node_id = f"run:gh:{live_run_id}"
                    await self._store.merge_node(
                        NodeType.RUN,
                        live_run_node_id,
                        {
                            "name": f"GitHub run {live_run_id}",
                            "live_run_id": live_run_id,
                            "source": "github_workflow",
                            "repo": repo_slug,
                        },
                    )
                    counts["runs"] += 1
                    await _belongs_to(NodeType.RUN, live_run_node_id)

            # Pass 2: edges. Both endpoints are guaranteed to exist
            # because pass 1 covered every node we'll merge against
            # (and tracked-PR/issue nodes are merged earlier in
            # full_sync).
            tracked_prs = set(state.tracked_prs.keys())
            tracked_issues = set(state.tracked_issues.keys())
            for event in causal_events:
                event_id = f"causal:{event.id}"
                event_valid_from = event.observed_at.isoformat() if event.observed_at else None

                # ── CAUSED_BY (parent chain) ────────────────────────
                if event.parent_id and causal_store.get(event.parent_id) is not None:
                    await self._store.merge_edge(
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        NodeType.CAUSAL_EVENT,
                        f"causal:{event.parent_id}",
                        RelType.CAUSED_BY,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1

                # ── ON (CausalEvent → PR/Issue/Comment) ─────────────
                ref_kind = event.ref.kind
                ref_number = event.ref.number
                if ref_kind == "pr" and ref_number is not None and ref_number in tracked_prs:
                    await self._store.merge_edge(
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        NodeType.PR,
                        f"pr:{ref_number}",
                        RelType.ON,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1
                elif (
                    ref_kind == "issue" and ref_number is not None and ref_number in tracked_issues
                ):
                    await self._store.merge_edge(
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        NodeType.ISSUE,
                        f"issue:{ref_number}",
                        RelType.ON,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1
                elif (
                    ref_kind == "comment"
                    and ref_number is not None
                    and event.ref.comment_id is not None
                ):
                    comment_id = f"comment:{ref_number}:{event.ref.comment_id}"
                    # CausalEvent → Comment (the marker lives on the comment).
                    await self._store.merge_edge(
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        NodeType.COMMENT,
                        comment_id,
                        RelType.ON,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1
                    # Comment → CausalEvent (1:1 today; 1:N once
                    # multi-marker bodies are supported).
                    await self._store.merge_edge(
                        NodeType.COMMENT,
                        comment_id,
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        RelType.EMITS,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1
                    # Comment → parent thread (PR or Issue, whichever
                    # is tracked). Without this link, cypher queries
                    # rooted at a PR/Issue can't reach comments
                    # directly.
                    if ref_number in tracked_prs:
                        await self._store.merge_edge(
                            NodeType.COMMENT,
                            comment_id,
                            NodeType.PR,
                            f"pr:{ref_number}",
                            RelType.ON,
                            _bitemporal(valid_from=event_valid_from),
                        )
                        counts["edges"] += 1
                    elif ref_number in tracked_issues:
                        await self._store.merge_edge(
                            NodeType.COMMENT,
                            comment_id,
                            NodeType.ISSUE,
                            f"issue:{ref_number}",
                            RelType.ON,
                            _bitemporal(valid_from=event_valid_from),
                        )
                        counts["edges"] += 1

                # ── HAS_EVENT (live :Run → :CausalEvent) ────────────
                live_run_id = parse_run_id(event.id)
                if live_run_id:
                    live_run_node_id = f"run:gh:{live_run_id}"
                    await self._store.merge_edge(
                        NodeType.RUN,
                        live_run_node_id,
                        NodeType.CAUSAL_EVENT,
                        event_id,
                        RelType.HAS_EVENT,
                        _bitemporal(valid_from=event_valid_from),
                    )
                    counts["edges"] += 1

        logger.info(
            "Graph sync complete: %d agents, %d PRs, %d issues, %d goals, "
            "%d skills, %d runs, %d causal events, %d comments, %d executors, %d edges",
            counts["agents"],
            counts["prs"],
            counts["issues"],
            counts["goals"],
            counts["skills"],
            counts["runs"],
            counts["causal_events"],
            counts["comments"],
            counts["executors"],
            counts["edges"],
        )
        return counts
