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
