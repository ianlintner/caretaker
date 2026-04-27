"""MongoDB-backed fleet registry store.

Same async surface as :class:`caretaker.fleet.store.FleetRegistryStore`
and :class:`caretaker.fleet.sqlite_store.SQLiteFleetRegistryStore`. The
Mongo backend exists because the SQLite store assumes a single replica
("The receiver is a single-replica deployment so a local file-backed
SQLite database is sufficient" — sqlite_store.py docstring), but the
MCP backend now runs 2 replicas. Two pods writing to a shared SQLite
file would corrupt; two pods writing to Mongo just work.

Schema
------

Two collections:

``fleet_clients``
    One document per repo, keyed by repo slug. Mirrors :class:`FleetClient`.
    Upserted on every heartbeat.

``fleet_heartbeats``
    Append-only history. After every insert we trim anything older than
    the most recent :data:`_HEARTBEAT_HISTORY_MAXLEN` rows for that repo,
    matching the in-memory ``deque(maxlen=32)`` semantics.

Connection URL is read from the env var named at construction (default
``MONGODB_URL``). The collections are created lazily on first use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .store import _HEARTBEAT_HISTORY_MAXLEN, FleetClient

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import motor.motor_asyncio


_DB = "caretaker"
_COL_CLIENTS = "fleet_clients"
_COL_HEARTBEATS = "fleet_heartbeats"


class MongoFleetRegistryStore:
    """Durable, multi-replica-safe fleet registry backed by MongoDB."""

    def __init__(
        self,
        *,
        mongodb_url: str | None = None,
        mongodb_url_env: str = "MONGODB_URL",
        database_name: str = _DB,
    ) -> None:
        url = mongodb_url if mongodb_url is not None else os.environ.get(mongodb_url_env, "")
        if not url:
            raise ValueError(
                f"MongoFleetRegistryStore requires a MongoDB URL "
                f"(set {mongodb_url_env} or pass mongodb_url=...)"
            )
        self._url = url
        self._database_name = database_name
        self._client: motor.motor_asyncio.AsyncIOMotorClient[Any] | None = None
        self._connect_lock = asyncio.Lock()
        self._indexes_created = False

    # ── Lifecycle ───────────────────────────────────────────────────

    async def _db(self) -> motor.motor_asyncio.AsyncIOMotorDatabase[Any]:
        if self._client is not None:
            return self._client[self._database_name]
        async with self._connect_lock:
            if self._client is None:
                from motor.motor_asyncio import AsyncIOMotorClient

                self._client = AsyncIOMotorClient(self._url)
        return self._client[self._database_name]

    async def _ensure_indexes(self) -> None:
        if self._indexes_created:
            return
        try:
            db = await self._db()
            import pymongo

            await db[_COL_CLIENTS].create_index(
                [("repo", pymongo.ASCENDING)],
                unique=True,
                name="idx_fleet_clients_repo",
            )
            await db[_COL_HEARTBEATS].create_index(
                [("repo", pymongo.ASCENDING), ("_id", pymongo.ASCENDING)],
                name="idx_fleet_heartbeats_repo_id",
            )
            self._indexes_created = True
        except Exception:
            logger.warning("MongoFleetRegistryStore index creation failed", exc_info=True)

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    # ── Public API ──────────────────────────────────────────────────

    async def record_heartbeat(self, payload: dict[str, Any]) -> FleetClient:
        repo = str(payload.get("repo") or "").strip()
        if not repo:
            raise ValueError("heartbeat missing 'repo'")

        await self._ensure_indexes()

        now = datetime.now(UTC)
        run_at = _parse_run_at(payload.get("run_at"), default=now)

        db = await self._db()
        clients = db[_COL_CLIENTS]
        heartbeats = db[_COL_HEARTBEATS]

        existing = await clients.find_one({"repo": repo})
        first_seen = (
            _parse_run_at(existing.get("first_seen"), default=run_at) if existing else run_at
        )
        heartbeats_seen = (existing.get("heartbeats_seen", 0) if existing else 0) + 1

        record = FleetClient(
            repo=repo,
            caretaker_version=str(payload.get("caretaker_version", "unknown")),
            last_seen=run_at,
            first_seen=first_seen,
            last_mode=str(payload.get("mode", "full")),
            enabled_agents=list(payload.get("enabled_agents") or []),
            last_goal_health=payload.get("goal_health"),
            last_error_count=int(payload.get("error_count") or 0),
            last_counters=dict(payload.get("counters") or {}),
            last_summary=payload.get("summary"),
            heartbeats_seen=heartbeats_seen,
        )

        await clients.replace_one(
            {"repo": repo},
            _client_to_doc(record),
            upsert=True,
        )

        snapshot = _snapshot(record, run_at=run_at)
        await heartbeats.insert_one(snapshot)

        # Trim history to the most recent _HEARTBEAT_HISTORY_MAXLEN rows.
        # Find the cutoff _id, then delete anything older.
        cursor = (
            heartbeats.find({"repo": repo}, {"_id": 1})
            .sort("_id", -1)
            .skip(_HEARTBEAT_HISTORY_MAXLEN)
            .limit(1)
        )
        async for cutoff in cursor:
            await heartbeats.delete_many({"repo": repo, "_id": {"$lt": cutoff["_id"]}})

        return record

    async def recent_heartbeats(
        self, repo: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        db = await self._db()
        # Push the limit + sort into MongoDB so we never read more than the
        # caller asked for. Long-lived repos accumulate heartbeats faster
        # than the trim job runs (especially during incident bursts), so a
        # naive ``find(...).sort(asc)`` would OOM the backend pod under
        # adversarial conditions. We sort descending + limit, then reverse
        # the small in-memory list to preserve the legacy oldest-first
        # contract callers depend on.
        if limit is not None and limit > 0:
            cursor = db[_COL_HEARTBEATS].find({"repo": repo}).sort("_id", -1).limit(limit)
            items: list[dict[str, Any]] = []
            async for doc in cursor:
                items.append(_strip_id(doc))
            items.reverse()
            return items

        # Unbounded mode — kept for parity with the in-memory store, which
        # returns the full ring buffer. Hard-cap at ``_HEARTBEAT_HISTORY_MAXLEN``
        # so a misuse can't OOM the receiver: by construction we never
        # persist more than that per repo anyway.
        cursor = (
            db[_COL_HEARTBEATS]
            .find({"repo": repo})
            .sort("_id", -1)
            .limit(_HEARTBEAT_HISTORY_MAXLEN)
        )
        items = [_strip_id(doc) async for doc in cursor]
        items.reverse()
        return items

    async def list_clients(self) -> list[FleetClient]:
        await self._ensure_indexes()
        db = await self._db()
        cursor = db[_COL_CLIENTS].find({}).sort("last_seen", -1)
        return [_doc_to_client(doc) async for doc in cursor]

    async def get_client(self, repo: str) -> FleetClient | None:
        await self._ensure_indexes()
        db = await self._db()
        doc = await db[_COL_CLIENTS].find_one({"repo": repo})
        return _doc_to_client(doc) if doc else None

    async def remove_client(self, repo: str) -> bool:
        await self._ensure_indexes()
        db = await self._db()
        result = await db[_COL_CLIENTS].delete_one({"repo": repo})
        await db[_COL_HEARTBEATS].delete_many({"repo": repo})
        return result.deleted_count > 0

    async def size(self) -> int:
        await self._ensure_indexes()
        db = await self._db()
        return await db[_COL_CLIENTS].count_documents({})

    async def stale_clients(self, *, threshold: timedelta) -> list[FleetClient]:
        """Return clients whose last heartbeat is older than ``threshold``.

        ``last_seen`` is persisted as an ISO-8601 UTC string (matching
        the rest of caretaker's Mongo schema for cross-collection
        readability via Compass / mongoexport). String comparison is
        correct here because every writer goes through
        :func:`_client_to_doc`, which always emits the canonical
        ``YYYY-MM-DDTHH:MM:SS+00:00`` form. The downside is that this
        query cannot leverage a datetime index — fleet size at our
        scale (tens of repos) makes that fine, but if the fleet grows
        past low thousands we should revisit storing ``last_seen`` as
        a native BSON ``Date`` and add a sparse index on it.
        """
        await self._ensure_indexes()
        cutoff = datetime.now(UTC) - threshold
        db = await self._db()
        cursor = db[_COL_CLIENTS].find({"last_seen": {"$lt": cutoff.isoformat()}})
        return [_doc_to_client(doc) async for doc in cursor]


# ── Serialisation helpers ─────────────────────────────────────────────


def _client_to_doc(record: FleetClient) -> dict[str, Any]:
    """Serialise a :class:`FleetClient` to a Mongo doc.

    Datetimes are stored as ISO-8601 strings to match the rest of the
    caretaker schema (run records, audit log) so cross-collection joins
    via Compass / mongoexport stay readable.
    """
    return {
        "repo": record.repo,
        "caretaker_version": record.caretaker_version,
        "last_seen": record.last_seen.isoformat(),
        "first_seen": record.first_seen.isoformat(),
        "last_mode": record.last_mode,
        "enabled_agents": list(record.enabled_agents),
        "last_goal_health": record.last_goal_health,
        "last_error_count": record.last_error_count,
        "last_counters": dict(record.last_counters),
        "last_summary": record.last_summary,
        "heartbeats_seen": record.heartbeats_seen,
    }


def _doc_to_client(doc: dict[str, Any]) -> FleetClient:
    return FleetClient(
        repo=doc["repo"],
        caretaker_version=doc.get("caretaker_version", "unknown"),
        last_seen=_parse_run_at(doc.get("last_seen"), default=datetime.now(UTC)),
        first_seen=_parse_run_at(doc.get("first_seen"), default=datetime.now(UTC)),
        last_mode=doc.get("last_mode", "full"),
        enabled_agents=list(doc.get("enabled_agents") or []),
        last_goal_health=doc.get("last_goal_health"),
        last_error_count=int(doc.get("last_error_count") or 0),
        last_counters=dict(doc.get("last_counters") or {}),
        last_summary=doc.get("last_summary"),
        heartbeats_seen=int(doc.get("heartbeats_seen") or 0),
    )


def _snapshot(record: FleetClient, *, run_at: datetime) -> dict[str, Any]:
    return {
        "repo": record.repo,
        "caretaker_version": record.caretaker_version,
        "run_at": run_at,
        "mode": record.last_mode,
        "enabled_agents": list(record.enabled_agents),
        "goal_health": record.last_goal_health,
        "error_count": record.last_error_count,
        "counters": dict(record.last_counters),
        "summary": record.last_summary,
    }


def _strip_id(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    out.pop("_id", None)
    return out


def _parse_run_at(value: Any, *, default: datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return default
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return default


__all__ = ["MongoFleetRegistryStore"]
