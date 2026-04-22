"""Graph node and edge type definitions for the Neo4j store."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    AGENT = "Agent"
    PR = "PR"
    ISSUE = "Issue"
    GOAL = "Goal"
    SKILL = "Skill"
    RUN = "Run"
    AUDIT_EVENT = "AuditEvent"
    MUTATION = "Mutation"
    CAUSAL_EVENT = "CausalEvent"
    # ── Added in M3 of the memory-graph plan ────────────────────────────
    # Tenant + attribution nodes. Every other node in the graph also
    # grows a ``repo`` scalar so cypher queries can scope via
    # ``WHERE n.repo = $repo``; the dedicated ``:Repo`` node is the
    # anchor point for the future ``BELONGS_TO`` edge and cross-repo
    # fleet queries. ``Comment`` and ``CheckRun`` complete the PR /
    # Issue attribution chain so "which comment spawned this chain?"
    # and "which check failed on this PR?" are one-hop queries.
    # ``Executor`` captures copilot / foundry / claude_code provenance
    # on the PR it handled.
    REPO = "Repo"
    COMMENT = "Comment"
    CHECK_RUN = "CheckRun"
    EXECUTOR = "Executor"
    # ── Added in M4 of the memory-graph plan ────────────────────────────
    # Tier-1 rollup node emitted by the nightly compaction job. Each
    # ``:RunSummaryWeek`` aggregates every ``:Run`` whose ``run_at``
    # falls inside one ISO week for a given repo so raw tier-0 rows
    # can be pruned after 30 days without losing weekly telemetry.
    # Skills that are crystallised out of a weekly rollup link back
    # via ``Skill-[:LEARNED_IN]->RunSummaryWeek`` provenance.
    RUN_SUMMARY_WEEK = "RunSummaryWeek"
    # ── Added in M6 of the memory-graph plan ────────────────────────────
    # Fleet-tier distilled procedural skill. Lives outside the per-repo
    # scope: one :GlobalSkill node can back many per-repo :Skill nodes
    # via PROMOTED_TO once a signature has been seen in N distinct
    # repos and passed the abstraction pass (see
    # ``caretaker.fleet.abstraction``). The label itself is the
    # privacy boundary — cypher queries against :Skill always see
    # per-repo data; queries against :GlobalSkill only ever see text
    # that has been run through the redactor.
    GLOBAL_SKILL = "GlobalSkill"
    # ── Added in M5 of the memory-graph plan ────────────────────────────
    # Per-agent working-memory node — one row per agent-run carrying the
    # identity / active-goal / recent-action-ring payload described in
    # §4.3. Written from the dispatch choke-point via the event-driven
    # :class:`~caretaker.graph.writer.GraphWriter` so agents publish the
    # fact they started a run without waiting on Neo4j.
    AGENT_CORE_MEMORY = "AgentCoreMemory"
    # ── T-E4: fleet alerts ───────────────────────────────────────────────
    # One ``:FleetAlert`` node per ``(repo, kind)`` tuple. The evaluator in
    # :mod:`caretaker.fleet.alerts` opens alerts when heartbeat metrics
    # regress and resolves them when the metric clears; both transitions
    # are mirrored into the graph via ``merge_node`` so cypher can answer
    # "which repos are currently alerting?" across the fleet.
    FLEET_ALERT = "FleetAlert"
    # ── Wave A3: self-heal incidents ─────────────────────────────────────
    # One ``:Incident`` node per self-heal dispatch — captures the
    # ``(repo, error_signature, kind)`` tuple plus the escalation path
    # (fix-ladder outcome, rungs tried). Wave B3 consumes these via the
    # memory retriever so escalation prompts can cite past incidents
    # with similar signatures. When an embedder is wired, a
    # ``summary_embedding`` is stamped so cosine ranking works without
    # a separate backfill.
    INCIDENT = "Incident"


class RelType(StrEnum):
    MONITORS = "MONITORS"
    TRIAGES = "TRIAGES"
    CONTRIBUTES_TO = "CONTRIBUTES_TO"
    LEARNED = "LEARNED"
    PERFORMED = "PERFORMED"
    LINKED_TO = "LINKED_TO"
    TRANSITIONED = "TRANSITIONED"
    DISPATCHED = "DISPATCHED"
    ESCALATED = "ESCALATED"
    PRODUCED = "PRODUCED"
    EVALUATED = "EVALUATED"
    MUTATED_BY = "MUTATED_BY"
    CAUSED_BY = "CAUSED_BY"
    # ── Added in M2 of the memory-graph plan ────────────────────────────
    # Richer, direction-aware edges so downstream queries don't have to
    # fan out through generic LINKED_TO / CONTRIBUTES_TO rels. Every
    # emitter is expected to stamp `observed_at` (automatic via the
    # writer) and, when known, bitemporal `valid_from` / `valid_to`.
    REFERENCES = "REFERENCES"  # PR → Issue (from "fixes #N" / TrackedIssue.assigned_pr)
    RESOLVED_BY = "RESOLVED_BY"  # Issue → PR
    EXECUTED = "EXECUTED"  # Run → Agent
    USED = "USED"  # Agent → Skill (per-run, distinct from LEARNED lifetime)
    VALIDATED_BY = "VALIDATED_BY"  # Skill → CausalEvent
    AFFECTED = "AFFECTED"  # Run → Goal (with before/after score)
    HANDLED_BY = "HANDLED_BY"  # PR → Executor (copilot / foundry / claude_code)
    # ── Added in M4 of the memory-graph plan ────────────────────────────
    # Skill → RunSummaryWeek provenance: lets callers answer "which
    # weekly rollup was this skill crystallised from?" once the
    # tier-0 → tier-1 compaction pass learns a new skill.
    LEARNED_IN = "LEARNED_IN"
    # ── Added in M6 of the memory-graph plan ────────────────────────────
    PROMOTED_TO = "PROMOTED_TO"  # Skill → GlobalSkill
    SHARES_SKILL = "SHARES_SKILL"  # Repo → GlobalSkill
    RUNS_AGENT = "RUNS_AGENT"  # Repo → Agent
    GOAL_HEALTH = "GOAL_HEALTH"  # Repo → Goal (with {score, as_of})
    # ── Added in M5 of the memory-graph plan ────────────────────────────
    # Per-agent working memory (``:AgentCoreMemory`` → ``:Agent``) —
    # mirrors the CoALA "working memory" scope. One edge per agent-run,
    # replaced in-place so the "current core memory" stays at the head.
    CORE_MEMORY_OF = "CORE_MEMORY_OF"


class GraphNode(BaseModel):
    """A node in the graph visualiser format."""

    id: str
    type: str
    label: str
    properties: dict[str, object] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """An edge in the graph visualiser format."""

    id: str
    source: str
    target: str
    type: str
    properties: dict[str, object] = Field(default_factory=dict)


class SubGraph(BaseModel):
    """A subgraph returned by query endpoints."""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class GraphStats(BaseModel):
    """Counts of nodes and edges by type."""

    node_counts: dict[str, int] = Field(default_factory=dict)
    edge_counts: dict[str, int] = Field(default_factory=dict)
    total_nodes: int = 0
    total_edges: int = 0
