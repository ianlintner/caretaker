"""Neo4j graph store — async connection and query operations."""

from __future__ import annotations

import logging
import os
from typing import Any

from caretaker.graph.models import GraphEdge, GraphNode, GraphStats, SubGraph

logger = logging.getLogger(__name__)


class GraphStore:
    """Async Neo4j driver wrapper for the caretaker graph."""

    def __init__(
        self,
        url: str | None = None,
        auth: tuple[str, str] | None = None,
        database: str = "neo4j",
    ) -> None:
        import neo4j

        self._url = url or os.environ.get("NEO4J_URL", "bolt://localhost:7687")
        if auth is None:
            raw = os.environ.get("NEO4J_AUTH", "neo4j/neo4j")
            parts = raw.split("/", 1)
            auth = (parts[0], parts[1]) if len(parts) == 2 else ("neo4j", raw)
        self._database = database
        self._driver = neo4j.AsyncGraphDatabase.driver(self._url, auth=auth)
        logger.info("GraphStore connected to %s (database=%s)", self._url, database)

    async def close(self) -> None:
        await self._driver.close()

    async def ensure_indexes(self) -> None:
        """Create indexes and constraints for performance."""
        queries = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (a:Agent) REQUIRE a.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:PR) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Issue) REQUIRE i.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (g:Goal) REQUIRE g.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (s:Skill) REQUIRE s.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (r:Run) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (e:AuditEvent) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Mutation) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:CausalEvent) REQUIRE c.id IS UNIQUE",
            # M3: tenant + attribution node constraints.
            "CREATE CONSTRAINT IF NOT EXISTS FOR (r:Repo) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Comment) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (cr:CheckRun) REQUIRE cr.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (e:Executor) REQUIRE e.id IS UNIQUE",
            # M4: tier-1 weekly rollup node constraint.
            "CREATE CONSTRAINT IF NOT EXISTS FOR (w:RunSummaryWeek) REQUIRE w.id IS UNIQUE",
            # M6: fleet-tier GlobalSkill. Uniqueness by ``id`` mirrors
            # every other label; the ``signature`` property carries the
            # cross-repo skill fingerprint (the abstracted SOP text is
            # stored on the node itself).
            "CREATE CONSTRAINT IF NOT EXISTS FOR (g:GlobalSkill) REQUIRE g.id IS UNIQUE",
        ]
        async with self._driver.session(database=self._database) as session:
            for q in queries:
                await session.run(q)
        logger.info("Graph indexes ensured")

    # ── Write operations ──────────────────────────────────────────────────

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None:
        """Create or update a node."""
        query = f"MERGE (n:{label} {{id: $id}}) SET n += $props"
        async with self._driver.session(database=self._database) as session:
            await session.run(query, id=node_id, props=properties)

    async def merge_edge(
        self,
        source_label: str,
        source_id: str,
        target_label: str,
        target_id: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Create or update a relationship."""
        query = (
            f"MATCH (a:{source_label} {{id: $src_id}}), (b:{target_label} {{id: $tgt_id}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += $props"
        )
        async with self._driver.session(database=self._database) as session:
            await session.run(query, src_id=source_id, tgt_id=target_id, props=properties or {})

    async def clear_all(self) -> None:
        """Delete all nodes and relationships (use with caution)."""
        async with self._driver.session(database=self._database) as session:
            await session.run("MATCH (n) DETACH DELETE n")
        logger.warning("Graph store cleared")

    # ── Compaction primitives (M4) ────────────────────────────────────────
    #
    # These are the minimum store-level hooks the tiered compaction job
    # needs so the raw cypher stays out of ``compaction.py`` and the fake
    # stores used by unit tests can mirror them without speaking Neo4j.

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw property dicts for every ``label`` node matching ``where``.

        ``where`` is an optional cypher fragment that may reference the node
        alias ``n`` (e.g. ``"n.repo = $repo"``). ``params`` are passed
        through to the driver. The returned list preserves insertion order
        from Neo4j and each entry is the plain ``dict(n)`` representation
        — callers that need degree or relationships should go through
        ``get_neighbors`` instead.
        """
        clause = f"WHERE {where} " if where else ""
        query = f"MATCH (n:{label}) {clause}RETURN n"
        rows: list[dict[str, Any]] = []
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **(params or {}))
            async for record in result:
                rows.append(dict(record["n"]))
        return rows

    async def delete_node(self, label: str, node_id: str) -> None:
        """Detach-delete a single node by ``id``.

        Used by the tiered compaction prune pass (M4). Missing nodes are a
        no-op — the query simply matches zero rows.
        """
        query = f"MATCH (n:{label} {{id: $id}}) DETACH DELETE n"
        async with self._driver.session(database=self._database) as session:
            await session.run(query, id=node_id)

    # ── Read operations ───────────────────────────────────────────────────

    async def get_nodes(
        self,
        node_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[GraphNode]:
        """Return nodes, optionally filtered by type."""
        if node_type:
            query = f"MATCH (n:{node_type}) RETURN n, labels(n) AS labels SKIP $offset LIMIT $limit"
        else:
            query = "MATCH (n) RETURN n, labels(n) AS labels SKIP $offset LIMIT $limit"

        nodes = []
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, offset=offset, limit=limit)
            async for record in result:
                n = record["n"]
                labels = record["labels"]
                props = dict(n)
                node_id = props.pop("id", str(n.element_id))
                label = props.get("name", props.get("number", node_id))
                nodes.append(
                    GraphNode(
                        id=str(node_id),
                        type=labels[0] if labels else "Unknown",
                        label=str(label),
                        properties=props,
                    )
                )
        return nodes

    async def get_neighbors(
        self,
        node_id: str,
        depth: int = 1,
    ) -> SubGraph:
        """Return the neighborhood subgraph around a node."""
        query = (
            "MATCH path = (start {id: $node_id})-[*1..$depth]-(neighbor) "
            "RETURN nodes(path) AS nodes, relationships(path) AS rels"
        )
        seen_nodes: dict[str, GraphNode] = {}
        seen_edges: dict[str, GraphEdge] = {}

        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, node_id=node_id, depth=depth)
            async for record in result:
                for n in record["nodes"]:
                    props = dict(n)
                    nid = str(props.pop("id", n.element_id))
                    if nid not in seen_nodes:
                        labels = list(n.labels)
                        seen_nodes[nid] = GraphNode(
                            id=nid,
                            type=labels[0] if labels else "Unknown",
                            label=str(props.get("name", props.get("number", nid))),
                            properties=props,
                        )
                for r in record["rels"]:
                    eid = str(r.element_id)
                    if eid not in seen_edges:
                        src_props = dict(r.start_node)
                        tgt_props = dict(r.end_node)
                        seen_edges[eid] = GraphEdge(
                            id=eid,
                            source=str(src_props.get("id", r.start_node.element_id)),
                            target=str(tgt_props.get("id", r.end_node.element_id)),
                            type=r.type,
                            properties=dict(r),
                        )

        return SubGraph(nodes=list(seen_nodes.values()), edges=list(seen_edges.values()))

    async def get_shortest_path(self, from_id: str, to_id: str) -> SubGraph:
        """Find the shortest path between two nodes."""
        query = (
            "MATCH path = shortestPath((a {id: $from_id})-[*]-(b {id: $to_id})) "
            "RETURN nodes(path) AS nodes, relationships(path) AS rels"
        )
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, from_id=from_id, to_id=to_id)
            record = await result.single()
            if record:
                for n in record["nodes"]:
                    props = dict(n)
                    nid = str(props.pop("id", n.element_id))
                    labels = list(n.labels)
                    nodes.append(
                        GraphNode(
                            id=nid,
                            type=labels[0] if labels else "Unknown",
                            label=str(props.get("name", props.get("number", nid))),
                            properties=props,
                        )
                    )
                for r in record["rels"]:
                    src_props = dict(r.start_node)
                    tgt_props = dict(r.end_node)
                    edges.append(
                        GraphEdge(
                            id=str(r.element_id),
                            source=str(src_props.get("id", r.start_node.element_id)),
                            target=str(tgt_props.get("id", r.end_node.element_id)),
                            type=r.type,
                            properties=dict(r),
                        )
                    )

        return SubGraph(nodes=nodes, edges=edges)

    async def get_subgraph(
        self,
        node_types: list[str] | None = None,
        limit: int = 200,
    ) -> SubGraph:
        """Return a filtered subgraph for visualisation."""
        if node_types:
            query = (
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $types) "
                "OPTIONAL MATCH (n)-[r]-(m) WHERE any(l IN labels(m) WHERE l IN $types) "
                "RETURN n, r, m LIMIT $limit"
            )
            params: dict[str, Any] = {"types": node_types, "limit": limit}
        else:
            query = "MATCH (n) OPTIONAL MATCH (n)-[r]-(m) RETURN n, r, m LIMIT $limit"
            params = {"limit": limit}

        seen_nodes: dict[str, GraphNode] = {}
        seen_edges: dict[str, GraphEdge] = {}

        async with self._driver.session(database=self._database) as session:
            result = await session.run(query, **params)
            async for record in result:
                for key in ("n", "m"):
                    node = record[key]
                    if node is not None:
                        props = dict(node)
                        nid = str(props.pop("id", node.element_id))
                        if nid not in seen_nodes:
                            labels = list(node.labels)
                            seen_nodes[nid] = GraphNode(
                                id=nid,
                                type=labels[0] if labels else "Unknown",
                                label=str(props.get("name", props.get("number", nid))),
                                properties=props,
                            )
                rel = record["r"]
                if rel is not None:
                    eid = str(rel.element_id)
                    if eid not in seen_edges:
                        src_props = dict(rel.start_node)
                        tgt_props = dict(rel.end_node)
                        seen_edges[eid] = GraphEdge(
                            id=eid,
                            source=str(src_props.get("id", rel.start_node.element_id)),
                            target=str(tgt_props.get("id", rel.end_node.element_id)),
                            type=rel.type,
                            properties=dict(rel),
                        )

        return SubGraph(nodes=list(seen_nodes.values()), edges=list(seen_edges.values()))

    async def list_skill_rows(self) -> list[dict[str, Any]]:
        """Return one row per ``:Skill`` node — ``{id, signature, repo, sop_text}``.

        Used by M6 fleet promotion to group skills by signature across
        all tenant subgraphs. Missing properties default to the empty
        string so the cross-repo grouper always has a scalar to hash
        on. Callers are responsible for filtering out ``Unknown``-
        tenant rows if they wish; the query deliberately returns them
        so the promotion audit log can see when a skill landed under
        the ``unknown/unknown`` placeholder.
        """
        rows: list[dict[str, Any]] = []
        query = (
            "MATCH (s:Skill) "
            "RETURN s.id AS id, "
            "       coalesce(s.signature, '') AS signature, "
            "       coalesce(s.repo, '') AS repo, "
            "       coalesce(s.name, '') AS name, "
            "       coalesce(s.category, '') AS category"
        )
        async with self._driver.session(database=self._database) as session:
            result = await session.run(query)
            async for record in result:
                rows.append(
                    {
                        "id": record["id"],
                        "signature": record["signature"],
                        "repo": record["repo"],
                        "name": record["name"],
                        "category": record["category"],
                    }
                )
        return rows

    async def get_stats(self) -> GraphStats:
        """Return node and edge counts by type."""
        stats = GraphStats()

        async with self._driver.session(database=self._database) as session:
            # Node counts
            result = await session.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS cnt")
            async for record in result:
                label = record["label"] or "Unknown"
                cnt = record["cnt"]
                stats.node_counts[label] = cnt
                stats.total_nodes += cnt

            # Edge counts
            result = await session.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(*) AS cnt"
            )
            async for record in result:
                rel_type = record["rel_type"]
                cnt = record["cnt"]
                stats.edge_counts[rel_type] = cnt
                stats.total_edges += cnt

        return stats
