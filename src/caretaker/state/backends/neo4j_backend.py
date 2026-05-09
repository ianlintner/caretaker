"""Neo4j MemoryBackend — graph-native agent memory via Neo4j.

Enabled when ``memory_store.backend = "neo4j"`` in ``.caretaker.yml``.
Uses the existing ``neo4j`` driver (already a core dependency).

Connection is read from ``NEO4J_URL`` and ``NEO4J_AUTH`` env vars
(same as the existing ``GraphStoreConfig``). This works with:

- **Neo4j Aura** (https://neo4j.com/cloud/) — free tier.
- **Neo4j Desktop** / local Docker: ``bolt://localhost:7687``.
- Any standard Neo4j instance.

Schema: each memory entry is a ``:MemoryEntry`` node with a unique
constraint on ``(namespace, key)``.  TTL is enforced in queries via
``WHERE m.expires_at IS NULL OR m.expires_at > datetime()``; a
periodic ``prune_expired()`` pass deletes expired nodes explicitly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import neo4j


logger = logging.getLogger(__name__)


class Neo4jMemoryBackend:
    """Neo4j-backed namespaced key-value store.

    Uses the synchronous ``neo4j`` driver so the interface matches
    ``MemoryBackend`` (no async leakage into agent code).

    Args:
        url: Bolt URL for the Neo4j instance.
        auth: ``(username, password)`` tuple.
        database: Neo4j database name.
        max_entries_per_namespace: Prune oldest entries when this limit is
            exceeded per namespace.  ``0`` disables the limit.
    """

    def __init__(
        self,
        url: str,
        auth: tuple[str, str],
        database: str = "neo4j",
        max_entries_per_namespace: int = 1000,
    ) -> None:
        try:
            import neo4j as _neo4j
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "neo4j is required for the Neo4j memory backend. "
                "It is already a core dependency of caretaker."
            ) from exc

        self._max_entries = max_entries_per_namespace
        self._database = database
        self._driver: neo4j.Driver = _neo4j.GraphDatabase.driver(url, auth=auth)
        self._ensure_schema()
        logger.info("Neo4jMemoryBackend connected (%s, database=%s)", _redact_url(url), database)

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create unique constraint and index if they don't already exist."""
        with self._driver.session(database=self._database) as session:
            # Unique constraint: one node per (namespace, key).
            session.run(
                """
                CREATE CONSTRAINT mem_ns_key IF NOT EXISTS
                FOR (m:MemoryEntry) REQUIRE (m.namespace, m.key) IS UNIQUE
                """
            )
            # Index for listing by namespace ordered by updated_at.
            session.run(
                """
                CREATE INDEX mem_ns_updated IF NOT EXISTS
                FOR (m:MemoryEntry) ON (m.namespace, m.updated_at)
                """
            )

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns, key: $key})
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                RETURN m.value
                """,
                ns=namespace,
                key=key,
                now=now,
            )
            record = result.single()
            if record is None:
                return None
            return str(record["m.value"])

    def get_json(self, namespace: str, key: str) -> Any:
        raw = self.get(namespace, key)
        if raw is None:
            return None
        return json.loads(raw)

    def list_keys(self, namespace: str) -> list[str]:
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns})
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                RETURN m.key
                ORDER BY m.updated_at DESC
                """,
                ns=namespace,
                now=now,
            )
            return [str(r["m.key"]) for r in result]

    def all_entries(self, namespace: str) -> dict[str, str]:
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns})
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                RETURN m.key, m.value
                ORDER BY m.updated_at DESC
                """,
                ns=namespace,
                now=now,
            )
            return {str(r["m.key"]): str(r["m.value"]) for r in result}

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        expires_at: str | None = None
        if ttl_seconds is not None:
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

        with self._driver.session(database=self._database) as session:
            session.run(
                """
                MERGE (m:MemoryEntry {namespace: $ns, key: $key})
                ON CREATE SET
                    m.value = $val,
                    m.created_at = $now,
                    m.updated_at = $now,
                    m.expires_at = $exp
                ON MATCH SET
                    m.value = $val,
                    m.updated_at = $now,
                    m.expires_at = $exp
                """,
                ns=namespace,
                key=key,
                val=value,
                now=now_iso,
                exp=expires_at,
            )
        if self._max_entries > 0:
            self._enforce_namespace_limit(namespace)

    def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        self.set(namespace, key, json.dumps(value), ttl_seconds=ttl_seconds)

    def delete(self, namespace: str, key: str) -> None:
        with self._driver.session(database=self._database) as session:
            session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns, key: $key})
                DETACH DELETE m
                """,
                ns=namespace,
                key=key,
            )

    # ── Maintenance ───────────────────────────────────────────────────────

    def _enforce_namespace_limit(self, namespace: str) -> None:
        """Prune oldest entries when the per-namespace cap is exceeded."""
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns})
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                RETURN count(m) AS cnt
                """,
                ns=namespace,
                now=now,
            )
            record = result.single()
            count = record["cnt"] if record else 0
            if count <= self._max_entries:
                return
            excess = count - self._max_entries
            session.run(
                """
                MATCH (m:MemoryEntry {namespace: $ns})
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                WITH m
                ORDER BY m.updated_at ASC
                LIMIT $excess
                DETACH DELETE m
                """,
                ns=namespace,
                now=now,
                excess=excess,
            )

    def prune_expired(self) -> int:
        """Delete all expired ``:MemoryEntry`` nodes.  Returns count removed."""
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry)
                WHERE m.expires_at IS NOT NULL AND m.expires_at <= $now
                WITH m, count(m) AS cnt
                DETACH DELETE m
                RETURN cnt
                """,
                now=now,
            )
            record = result.single()
            return record["cnt"] if record else 0

    def snapshot_json(self) -> str:
        """Return all non-expired entries as a JSON string (for workflow artifacts)."""
        now = datetime.now(UTC).isoformat()
        with self._driver.session(database=self._database) as session:
            result = session.run(
                """
                MATCH (m:MemoryEntry)
                WHERE m.expires_at IS NULL OR m.expires_at > $now
                RETURN m.namespace, m.key, m.value,
                       m.created_at, m.updated_at, m.expires_at
                ORDER BY m.namespace, m.updated_at DESC
                """,
                now=now,
            )
            data: dict[str, list[dict[str, str | None]]] = {}
            for r in result:
                ns = str(r["m.namespace"])
                data.setdefault(ns, []).append(
                    {
                        "key": str(r["m.key"]),
                        "value": str(r["m.value"]),
                        "created_at": str(r["m.created_at"]) if r["m.created_at"] else None,
                        "updated_at": str(r["m.updated_at"]) if r["m.updated_at"] else None,
                        "expires_at": str(r["m.expires_at"]) if r["m.expires_at"] else None,
                    }
                )
            return json.dumps(data, indent=2)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._driver.close()


def _redact_url(url: str) -> str:
    """Return a safe-for-logs version of the Bolt URL (credentials redacted)."""
    if "@" in url:
        prefix, rest = url.split("@", 1)
        if "://" in prefix:
            scheme, creds = prefix.split("://", 1)
            return f"{scheme}://****@{rest}"
    return url


def build_neo4j_backend(
    url_env: str = "NEO4J_URL",
    auth_env: str = "NEO4J_AUTH",
    database: str = "neo4j",
    max_entries_per_namespace: int = 1000,
) -> Neo4jMemoryBackend:
    """Construct a ``Neo4jMemoryBackend`` from environment variables.

    Raises ``RuntimeError`` if the required env vars are unset.
    """
    url = os.environ.get(url_env, "").strip()
    if not url:
        raise RuntimeError(
            f"Environment variable '{url_env}' is not set. "
            "Set it to a Neo4j Bolt URL, e.g.:\n"
            "  bolt://localhost:7687   (local Docker / Desktop)\n"
            "  neo4j+s://dbid.databases.neo4j.io  (Aura)"
        )

    raw_auth = os.environ.get(auth_env, "").strip()
    if not raw_auth:
        raise RuntimeError(
            f"Environment variable '{auth_env}' is not set. "
            "Set it to 'username/password', e.g. 'neo4j/password123'"
        )
    parts = raw_auth.split("/", 1)
    auth = (parts[0], parts[1]) if len(parts) == 2 else ("neo4j", raw_auth)

    return Neo4jMemoryBackend(
        url=url,
        auth=auth,
        database=database,
        max_entries_per_namespace=max_entries_per_namespace,
    )
