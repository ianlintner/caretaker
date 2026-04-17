"""MongoDB MemoryBackend — Azure Cosmos DB for MongoDB / Atlas / local mongod.

Enabled when ``memory_store.backend = "mongo"`` in ``.caretaker.yml``.
Requires the ``motor`` package (which pulls in ``pymongo``), installed via the
``backend`` extra: ``pip install caretaker[backend]``.

Connection URL is read from the env var named in
``mongo.mongodb_url_env`` (default: ``MONGODB_URL``). This works with:

- **Azure Cosmos DB for MongoDB** — always-free tier: 1,000 RU/s + 25 GB.
  Connection string from Azure Portal starts with ``mongodb+srv://...``.
- **MongoDB Atlas** (https://www.mongodb.com/atlas) — M0 free cluster.
- Any standard ``mongod`` instance: ``mongodb://localhost:27017/``.

Schema is document-based (no migrations). A compound unique index on
``(namespace, key)`` is created on first connection.  If ``expires_at`` is
set on a document, MongoDB's TTL index deletes it automatically — no
manual ``prune_expired()`` scan needed (though the method is kept for
Protocol compliance and returns 0).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import pymongo
    import pymongo.collection


class MongoMemoryBackend:
    """MongoDB-backed namespaced key-value store.

    Uses the synchronous ``pymongo`` driver so the interface matches
    ``SQLiteMemoryBackend`` (no async leakage into agent code).  ``motor``
    (which is listed as our dep) installs ``pymongo`` automatically.

    Args:
        mongodb_url: Standard MongoDB connection URI.
        database_name: MongoDB database to use.
        collection_name: Collection for agent memory documents.
        max_entries_per_namespace: Prune oldest entries when this limit is
            exceeded per namespace.  ``0`` disables the limit.
    """

    def __init__(
        self,
        mongodb_url: str,
        database_name: str = "caretaker",
        collection_name: str = "agent_memory",
        max_entries_per_namespace: int = 1000,
    ) -> None:
        try:
            import pymongo as _pymongo
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pymongo is required for the MongoDB memory backend. "
                "Install it with: pip install caretaker[backend]"
            ) from exc

        self._max_entries = max_entries_per_namespace
        self._client: pymongo.MongoClient[Any] = _pymongo.MongoClient(mongodb_url)
        self._col: pymongo.collection.Collection[Any] = self._client[database_name][collection_name]
        self._ensure_indexes()
        logger.info("MongoMemoryBackend connected (db=%s col=%s)", database_name, collection_name)

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def _ensure_indexes(self) -> None:
        """Create compound unique + TTL indexes if they do not already exist."""
        import pymongo

        # Compound unique index: fast lookup and upsert guard.
        self._col.create_index(
            [("namespace", pymongo.ASCENDING), ("key", pymongo.ASCENDING)],
            unique=True,
            name="idx_ns_key",
        )
        # TTL index: MongoDB daemon auto-deletes documents when expires_at passes.
        # background=True avoids blocking other ops during index creation.
        self._col.create_index(
            [("expires_at", pymongo.ASCENDING)],
            expireAfterSeconds=0,
            name="idx_ttl_expires",
        )
        # Secondary index for newest-first listing within a namespace.
        self._col.create_index(
            [("namespace", pymongo.ASCENDING), ("updated_at", pymongo.DESCENDING)],
            name="idx_ns_updated",
        )

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        now = datetime.now(UTC)
        doc = self._col.find_one(
            {"namespace": namespace, "key": key},
            {"value": 1, "expires_at": 1},
        )
        if doc is None:
            return None
        expires_at = doc.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                # MongoDB TTL index will clean this up; help it along immediately.
                self.delete(namespace, key)
                return None
        return str(doc["value"])

    def get_json(self, namespace: str, key: str) -> Any:
        raw = self.get(namespace, key)
        if raw is None:
            return None
        return json.loads(raw)

    def list_keys(self, namespace: str) -> list[str]:
        now = datetime.now(UTC)
        cursor = self._col.find(
            {
                "namespace": namespace,
                "$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}],
            },
            {"key": 1},
            sort=[("updated_at", -1)],
        )
        return [doc["key"] for doc in cursor]

    def all_entries(self, namespace: str) -> dict[str, str]:
        now = datetime.now(UTC)
        cursor = self._col.find(
            {
                "namespace": namespace,
                "$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}],
            },
            {"key": 1, "value": 1},
        )
        return {doc["key"]: str(doc["value"]) for doc in cursor}

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        now = datetime.now(UTC)
        expires_at: datetime | None = None
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)

        self._col.update_one(
            {"namespace": namespace, "key": key},
            {
                "$set": {
                    "value": value,
                    "updated_at": now,
                    "expires_at": expires_at,
                },
                "$setOnInsert": {
                    "namespace": namespace,
                    "key": key,
                    "created_at": now,
                },
            },
            upsert=True,
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
        self._col.delete_one({"namespace": namespace, "key": key})

    # ── Maintenance ───────────────────────────────────────────────────────

    def _enforce_namespace_limit(self, namespace: str) -> None:
        """Prune the oldest entries when the per-namespace cap is exceeded."""
        count = self._col.count_documents({"namespace": namespace})
        if count <= self._max_entries:
            return
        excess = count - self._max_entries
        # Find the oldest entry IDs to delete.
        oldest = list(
            self._col.find(
                {"namespace": namespace},
                {"_id": 1},
                sort=[("updated_at", 1)],
                limit=excess,
            )
        )
        if oldest:
            ids = [doc["_id"] for doc in oldest]
            self._col.delete_many({"_id": {"$in": ids}})

    def prune_expired(self) -> int:
        """No-op: MongoDB TTL index handles expiry automatically.

        Returns 0 (Protocol compliance).  Kept so code that calls
        ``backend.prune_expired()`` does not break when the backend is swapped.
        """
        return 0

    def snapshot_json(self) -> str:
        """Return all non-expired entries as a JSON string (for workflow artifacts)."""
        now = datetime.now(UTC)
        cursor = self._col.find(
            {"$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}]},
            sort=[("namespace", 1), ("updated_at", -1)],
        )
        data: dict[str, list[dict[str, str | None]]] = {}
        for doc in cursor:
            ns = doc["namespace"]
            data.setdefault(ns, []).append(
                {
                    "key": doc["key"],
                    "value": str(doc["value"]),
                    "created_at": doc.get("created_at", "").isoformat()
                    if doc.get("created_at")
                    else None,
                    "updated_at": doc.get("updated_at", "").isoformat()
                    if doc.get("updated_at")
                    else None,
                    "expires_at": doc.get("expires_at", "").isoformat()
                    if doc.get("expires_at")
                    else None,
                }
            )
        return json.dumps(data, indent=2)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()


def build_mongo_backend(
    mongodb_url_env: str = "MONGODB_URL",
    database_name: str = "caretaker",
    collection_name: str = "agent_memory",
    max_entries_per_namespace: int = 1000,
) -> MongoMemoryBackend:
    """Construct a ``MongoMemoryBackend`` from environment variables.

    Raises ``RuntimeError`` if the required env var is unset.
    """
    mongodb_url = os.environ.get(mongodb_url_env, "").strip()
    if not mongodb_url:
        raise RuntimeError(
            f"Environment variable '{mongodb_url_env}' is not set. "
            "Set it to a MongoDB connection URI, e.g.:\n"
            "  mongodb+srv://user:pass@cluster.cosmos.azure.com/?tls=true  (Cosmos DB)\n"
            "  mongodb://localhost:27017/                                    (local)"
        )
    return MongoMemoryBackend(
        mongodb_url=mongodb_url,
        database_name=database_name,
        collection_name=collection_name,
        max_entries_per_namespace=max_entries_per_namespace,
    )
