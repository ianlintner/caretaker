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
