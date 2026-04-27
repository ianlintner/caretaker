"""Storage layer for streamed runs.

Two collaborating stores:

* **Redis Stream** ``runs:{id}:stream`` — live fan-out + short-term replay
  (configurable MAXLEN, configurable retention). Both the runner-side
  shipper and the admin SSE endpoint read from this stream so there is
  exactly one wire-level format.

* **MongoDB** ``runs`` collection — durable archive of run records and
  final summary. Long-form replay (>24h) reads from Mongo, not Redis.

Both backends are optional: if neither is configured the store falls
back to an in-memory implementation suitable for local dev / tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.runs.models import (
    LogEntry,
    LogStream,
    RunRecord,
    RunStatus,
    RunSummaryView,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import motor.motor_asyncio
    import redis.asyncio

logger = logging.getLogger(__name__)


_DEFAULT_STREAM_MAXLEN = 50_000
_DEFAULT_RETENTION_HOURS = 24
_RUN_STREAM_KEY = "runs:{run_id}:stream"
_RUN_CURSOR_KEY = "runs:{run_id}:cursor"


def _stream_key(run_id: str) -> str:
    return _RUN_STREAM_KEY.format(run_id=run_id)


def _cursor_key(run_id: str) -> str:
    return _RUN_CURSOR_KEY.format(run_id=run_id)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _entry_to_redis_fields(entry: LogEntry) -> dict[str, str]:
    """Encode a :class:`LogEntry` for ``XADD`` (all values must be strings)."""
    return {
        "seq": str(entry.seq),
        "ts": entry.ts.astimezone(UTC).isoformat(),
        "stream": entry.stream.value,
        "data": entry.data,
        "tags": json.dumps(entry.tags) if entry.tags else "{}",
    }


def _redis_fields_to_entry(fields: dict[bytes | str, bytes | str]) -> LogEntry:
    def _decode(v: bytes | str) -> str:
        return v.decode("utf-8") if isinstance(v, bytes) else v

    raw: dict[str, str] = {_decode(k): _decode(v) for k, v in fields.items()}
    try:
        tags = json.loads(raw.get("tags", "{}")) or {}
    except json.JSONDecodeError:
        tags = {}
    ts_raw = raw.get("ts")
    return LogEntry(
        seq=int(raw.get("seq", "0")),
        ts=datetime.fromisoformat(ts_raw) if ts_raw else _utcnow(),
        stream=LogStream(raw.get("stream", LogStream.STDOUT.value)),
        data=raw.get("data", ""),
        tags=tags,
    )


# ---------------------------------------------------------------------------
# RunsStore
# ---------------------------------------------------------------------------


class RunsStore:
    """Durable + streaming storage for streamed runs.

    Configured via env vars:

    * ``MONGODB_URL`` — when set, run records archive to Mongo.
    * ``REDIS_URL`` — when set, log streams use Redis Streams.
    * ``CARETAKER_RUNS_REDIS_STREAM_MAXLEN`` — XADD MAXLEN bound (default 50000).
    * ``CARETAKER_RUNS_RETENTION_HOURS`` — Redis stream retention (default 24h).

    Both backends are independent: Redis-only + Mongo-only deployments
    work; the in-memory fallback covers local dev.
    """

    def __init__(
        self,
        *,
        mongodb_url: str = "",
        redis_url: str = "",
        database_name: str = "caretaker",
        collection_name: str = "runs",
        stream_maxlen: int = _DEFAULT_STREAM_MAXLEN,
        retention_hours: int = _DEFAULT_RETENTION_HOURS,
    ) -> None:
        self._mongodb_url = mongodb_url
        self._redis_url = redis_url
        self._database_name = database_name
        self._collection_name = collection_name
        self._stream_maxlen = stream_maxlen
        self._retention_hours = retention_hours

        self._mongo_client: motor.motor_asyncio.AsyncIOMotorClient[Any] | None = None
        self._redis: redis.asyncio.Redis[bytes] | None = None
        self._index_created = False

        # In-memory fallback (used when either backend is unavailable)
        self._mem_runs: dict[str, RunRecord] = {}
        self._mem_streams: dict[str, list[tuple[str, LogEntry]]] = defaultdict(list)
        self._mem_cursors: dict[str, int] = defaultdict(int)
        self._mem_lock = asyncio.Lock()
        # Per-run notification events for in-memory tail loops.
        self._mem_events: dict[str, asyncio.Event] = {}

    # ── Lifecycle ───────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> RunsStore:
        return cls(
            mongodb_url=os.environ.get("MONGODB_URL", "").strip(),
            redis_url=os.environ.get("REDIS_URL", "").strip(),
            stream_maxlen=int(
                os.environ.get(
                    "CARETAKER_RUNS_REDIS_STREAM_MAXLEN",
                    str(_DEFAULT_STREAM_MAXLEN),
                )
            ),
            retention_hours=int(
                os.environ.get(
                    "CARETAKER_RUNS_RETENTION_HOURS",
                    str(_DEFAULT_RETENTION_HOURS),
                )
            ),
        )

    async def close(self) -> None:
        if self._mongo_client is not None:
            with contextlib.suppress(Exception):
                self._mongo_client.close()
            self._mongo_client = None
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.close()
            self._redis = None

    async def _collection(
        self,
    ) -> motor.motor_asyncio.AsyncIOMotorCollection[dict[str, Any]] | None:
        if not self._mongodb_url:
            return None
        if self._mongo_client is None:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient

                self._mongo_client = AsyncIOMotorClient(self._mongodb_url)
            except Exception:
                logger.warning("RunsStore: Mongo connect failed", exc_info=True)
                return None
        col = self._mongo_client[self._database_name][self._collection_name]
        if not self._index_created:
            with contextlib.suppress(Exception):
                import pymongo

                await col.create_index(
                    [("run_id", pymongo.ASCENDING)],
                    unique=True,
                    name="idx_run_id",
                )
                await col.create_index(
                    [
                        ("repository_id", pymongo.ASCENDING),
                        ("gh_run_id", pymongo.ASCENDING),
                        ("gh_run_attempt", pymongo.ASCENDING),
                    ],
                    unique=True,
                    name="idx_natural_key",
                )
                await col.create_index(
                    [("started_at", pymongo.DESCENDING)],
                    name="idx_started_at",
                )
                self._index_created = True
        return col

    async def _redis_client(self) -> redis.asyncio.Redis[bytes] | None:
        if not self._redis_url:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis

                self._redis = aioredis.from_url(self._redis_url)
            except Exception:
                logger.warning("RunsStore: Redis connect failed", exc_info=True)
                return None
        return self._redis

    # ── Run records ─────────────────────────────────────────────────

    async def find_by_natural_key(
        self,
        *,
        repository_id: int,
        gh_run_id: int,
        gh_run_attempt: int,
    ) -> RunRecord | None:
        col = await self._collection()
        if col is not None:
            doc = await col.find_one(
                {
                    "repository_id": repository_id,
                    "gh_run_id": gh_run_id,
                    "gh_run_attempt": gh_run_attempt,
                }
            )
            if doc:
                doc.pop("_id", None)
                return RunRecord.model_validate(doc)
            return None

        async with self._mem_lock:
            for record in self._mem_runs.values():
                if (
                    record.repository_id == repository_id
                    and record.gh_run_id == gh_run_id
                    and record.gh_run_attempt == gh_run_attempt
                ):
                    return record
        return None

    async def create_run(self, record: RunRecord) -> RunRecord:
        """Insert a new run record. Idempotent: if a record with the same
        natural key already exists, return the existing one."""
        existing = await self.find_by_natural_key(
            repository_id=record.repository_id,
            gh_run_id=record.gh_run_id,
            gh_run_attempt=record.gh_run_attempt,
        )
        if existing is not None:
            return existing

        col = await self._collection()
        if col is not None:
            doc = record.model_dump(mode="json")
            try:
                await col.insert_one(doc)
            except Exception:
                # Race on natural-key uniqueness — re-fetch and return.
                existing = await self.find_by_natural_key(
                    repository_id=record.repository_id,
                    gh_run_id=record.gh_run_id,
                    gh_run_attempt=record.gh_run_attempt,
                )
                if existing is not None:
                    return existing
                raise
            return record

        async with self._mem_lock:
            self._mem_runs[record.run_id] = record
        return record

    async def get_run(self, run_id: str) -> RunRecord | None:
        col = await self._collection()
        if col is not None:
            doc = await col.find_one({"run_id": run_id})
            if doc:
                doc.pop("_id", None)
                return RunRecord.model_validate(doc)
            return None
        async with self._mem_lock:
            return self._mem_runs.get(run_id)

    async def update_run(self, run_id: str, **updates: Any) -> RunRecord | None:
        col = await self._collection()
        if col is not None:
            jsonable: dict[str, Any] = {}
            for k, v in updates.items():
                if isinstance(v, datetime):
                    jsonable[k] = v.astimezone(UTC).isoformat()
                elif isinstance(v, RunStatus):
                    jsonable[k] = v.value
                else:
                    jsonable[k] = v
            await col.update_one({"run_id": run_id}, {"$set": jsonable})
            return await self.get_run(run_id)

        async with self._mem_lock:
            existing = self._mem_runs.get(run_id)
            if existing is None:
                return None
            data = existing.model_dump()
            data.update(updates)
            updated = RunRecord.model_validate(data)
            self._mem_runs[run_id] = updated
            return updated

    async def list_runs(
        self,
        *,
        repository: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[RunSummaryView]:
        col = await self._collection()
        if col is not None:
            query: dict[str, Any] = {}
            if repository:
                query["repository"] = repository
            if status is not None:
                query["status"] = status.value
            if since is not None:
                query["started_at"] = {"$gte": since.astimezone(UTC).isoformat()}
            cursor = col.find(query).sort("started_at", -1).limit(int(limit))
            out: list[RunSummaryView] = []
            async for doc in cursor:
                doc.pop("_id", None)
                rec = RunRecord.model_validate(doc)
                out.append(_to_summary(rec))
            return out

        async with self._mem_lock:
            records = list(self._mem_runs.values())
        if repository:
            records = [r for r in records if r.repository == repository]
        if status is not None:
            records = [r for r in records if r.status == status]
        if since is not None:
            records = [r for r in records if r.started_at >= since]
        records.sort(key=lambda r: r.started_at, reverse=True)
        return [_to_summary(r) for r in records[: int(limit)]]

    # ── Stream operations ──────────────────────────────────────────

    async def append_log(self, run_id: str, entry: LogEntry) -> bool:
        """Append a log entry to the run stream. Returns True if accepted,
        False if the entry was a duplicate (seq <= cursor)."""
        client = await self._redis_client()
        if client is not None:
            cursor_key = _cursor_key(run_id)
            # Idempotency: only advance cursor when seq strictly increases.
            cur_raw = await client.get(cursor_key)
            current = int(cur_raw) if cur_raw else 0
            if entry.seq <= current and entry.seq != 0:
                return False
            await client.xadd(
                _stream_key(run_id),
                _entry_to_redis_fields(entry),
                maxlen=self._stream_maxlen,
                approximate=True,
            )
            if entry.seq > current:
                # NB: SET overwrites; race with concurrent appends would
                # rewind the cursor. Guard with WATCH/MULTI in production
                # if multi-writer correctness becomes critical.
                await client.set(
                    cursor_key,
                    str(entry.seq),
                    ex=self._retention_hours * 3600,
                )
            return True

        async with self._mem_lock:
            current = self._mem_cursors.get(run_id, 0)
            if entry.seq <= current and entry.seq != 0:
                return False
            stream_id = f"{int(_utcnow().timestamp() * 1000)}-{len(self._mem_streams[run_id])}"
            self._mem_streams[run_id].append((stream_id, entry))
            if entry.seq > current:
                self._mem_cursors[run_id] = entry.seq
            event = self._mem_events.setdefault(run_id, asyncio.Event())
        event.set()
        return True

    async def get_cursor(self, run_id: str) -> int:
        client = await self._redis_client()
        if client is not None:
            raw = await client.get(_cursor_key(run_id))
            return int(raw) if raw else 0
        async with self._mem_lock:
            return self._mem_cursors.get(run_id, 0)

    async def read_history(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[tuple[str, LogEntry]]:
        """Return log entries with ``seq > after_seq`` (replay window)."""
        client = await self._redis_client()
        if client is not None:
            raw = await client.xrange(_stream_key(run_id), min="-", max="+", count=limit)
            out: list[tuple[str, LogEntry]] = []
            for stream_id_b, fields in raw:
                stream_id = (
                    stream_id_b.decode("utf-8")
                    if isinstance(stream_id_b, bytes)
                    else str(stream_id_b)
                )
                entry = _redis_fields_to_entry(fields)
                if entry.seq > after_seq:
                    out.append((stream_id, entry))
            return out
        async with self._mem_lock:
            entries = list(self._mem_streams.get(run_id, []))
        return [(sid, e) for sid, e in entries if e.seq > after_seq][:limit]

    async def tail(
        self,
        run_id: str,
        *,
        last_stream_id: str = "$",
        block_ms: int = 15000,
    ) -> AsyncIterator[tuple[str, LogEntry] | None]:
        """Async iterator yielding ``(stream_id, LogEntry)`` as new entries land.

        ``last_stream_id="$"`` means "only entries newer than now". Pass a
        prior stream id (returned from ``read_history`` / earlier ``tail``)
        to resume after a reconnect.
        """
        client = await self._redis_client()
        cursor = last_stream_id
        if client is not None:
            while True:
                resp = await client.xread(
                    {_stream_key(run_id): cursor},
                    block=block_ms,
                    count=100,
                )
                if not resp:
                    yield None  # heartbeat sentinel for SSE keepalive
                    continue
                for _stream, items in resp:
                    for stream_id_b, fields in items:
                        stream_id = (
                            stream_id_b.decode("utf-8")
                            if isinstance(stream_id_b, bytes)
                            else str(stream_id_b)
                        )
                        cursor = stream_id
                        yield stream_id, _redis_fields_to_entry(fields)
            return

        # In-memory fallback: index-based tail.
        async with self._mem_lock:
            entries = list(self._mem_streams.get(run_id, []))
        # Resolve initial index from cursor.
        if cursor == "$":
            idx = len(entries)
        else:
            idx = next(
                (i for i, (sid, _) in enumerate(entries) if sid > cursor),
                len(entries),
            )
        while True:
            async with self._mem_lock:
                entries = list(self._mem_streams.get(run_id, []))
                event = self._mem_events.setdefault(run_id, asyncio.Event())
            if idx < len(entries):
                while idx < len(entries):
                    sid, entry = entries[idx]
                    idx += 1
                    yield sid, entry
                continue
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=block_ms / 1000.0)
            except TimeoutError:
                yield None  # heartbeat sentinel


def _to_summary(record: RunRecord) -> RunSummaryView:
    return RunSummaryView(
        run_id=record.run_id,
        repository=record.repository,
        actor=record.actor,
        event_name=record.event_name,
        mode=record.mode,
        status=record.status,
        started_at=record.started_at,
        finished_at=record.finished_at,
        exit_code=record.exit_code,
        last_seq=record.last_seq,
        last_heartbeat_at=record.last_heartbeat_at,
        workflow=record.workflow,
        sha=record.sha,
    )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_store: RunsStore | None = None


def get_store() -> RunsStore:
    global _store  # noqa: PLW0603
    if _store is None:
        _store = RunsStore.from_env()
    return _store


def set_store(store: RunsStore | None) -> None:
    """Override the singleton (tests)."""
    global _store  # noqa: PLW0603
    _store = store


def new_run_id() -> str:
    return uuid.uuid4().hex


__all__ = [
    "RunsStore",
    "get_store",
    "new_run_id",
    "set_store",
]
