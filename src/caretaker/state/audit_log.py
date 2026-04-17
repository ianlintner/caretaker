"""Audit log writer for agent decisions.

Writes structured audit records to:
- A MongoDB ``audit_log`` collection when ``audit_log.enabled = true`` and
  ``mongo.enabled = true`` (Phase 1 SaaS backend).
- A structured log line (JSON) in all cases for log-aggregation systems.

MongoDB creates the collection automatically on first write — no migrations
required.  A unique index on ``id`` is created on first connection.

Usage::

    audit = AuditLogWriter.from_config(config)
    await audit.record(
        run_id="run-abc",
        agent_id="security",
        tool="GitHub.list_issues",
        outcome="success",
        latency_ms=240,
    )
    await audit.close()
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import motor.motor_asyncio


@dataclass
class AuditRecord:
    """A single agent-decision audit record."""

    run_id: str
    agent_id: str
    outcome: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tool: str | None = None
    llm_model: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    prompt_id: str | None = None
    response_id: str | None = None
    extra: dict[str, Any] | None = None


class AuditLogWriter:
    """Write audit records to MongoDB and/or structured logging.

    Parameters
    ----------
    enabled:
        When ``False`` only structured-log output is produced; no MongoDB
        writes are performed.
    mongodb_url_env:
        Name of the environment variable containing the ``MONGODB_URL``.
    database_name:
        MongoDB database name.
    collection_name:
        MongoDB collection name for audit documents.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        mongodb_url_env: str = "MONGODB_URL",
        database_name: str = "caretaker",
        collection_name: str = "audit_log",
    ) -> None:
        self._enabled = enabled
        self._mongodb_url_env = mongodb_url_env
        self._database_name = database_name
        self._collection_name = collection_name
        self._client: motor.motor_asyncio.AsyncIOMotorClient[Any] | None = None
        self._index_created = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def _ensure_collection(  # noqa: E501
        self,
    ) -> motor.motor_asyncio.AsyncIOMotorCollection[dict[str, Any]] | None:
        """Return a live Motor collection handle, or None if unavailable."""
        if not self._enabled:
            return None

        mongodb_url = os.environ.get(self._mongodb_url_env, "").strip()
        if not mongodb_url:
            return None

        if self._client is None:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient

                self._client = AsyncIOMotorClient(mongodb_url)
                logger.debug("AuditLogWriter: connected to MongoDB")
            except Exception:
                logger.warning(
                    "AuditLogWriter: failed to connect to MongoDB; "
                    "audit records will not be persisted.",
                    exc_info=True,
                )
                return None

        col = self._client[self._database_name][self._collection_name]
        if not self._index_created:
            with contextlib.suppress(Exception):
                import pymongo

                await col.create_index([("id", pymongo.ASCENDING)], unique=True, name="idx_id")
                self._index_created = True
        return col

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        self._client = None

    # ── Public API ─────────────────────────────────────────────────────

    async def record(
        self,
        run_id: str,
        agent_id: str,
        outcome: str,
        *,
        tool: str | None = None,
        llm_model: str | None = None,
        latency_ms: int | None = None,
        cost_usd: float | None = None,
        prompt_id: str | None = None,
        response_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write one audit record.

        Always emits a structured log line.  Also persists to MongoDB when
        ``enabled=True`` and the database is reachable.
        """
        rec = AuditRecord(
            run_id=run_id,
            agent_id=agent_id,
            outcome=outcome,
            tool=tool,
            llm_model=llm_model,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            prompt_id=prompt_id,
            response_id=response_id,
            extra=extra,
        )
        self._emit_log(rec)
        await self._persist(rec)

    def _emit_log(self, rec: AuditRecord) -> None:
        logger.info(
            "audit",
            extra={
                "audit": True,
                "id": rec.id,
                "run_id": rec.run_id,
                "agent_id": rec.agent_id,
                "tool": rec.tool,
                "llm_model": rec.llm_model,
                "latency_ms": rec.latency_ms,
                "cost_usd": rec.cost_usd,
                "outcome": rec.outcome,
                "prompt_id": rec.prompt_id,
                "response_id": rec.response_id,
                "extra": rec.extra,
            },
        )

    async def _persist(self, rec: AuditRecord) -> None:
        col = await self._ensure_collection()
        if col is None:
            return
        doc = {
            "id": rec.id,
            "run_id": rec.run_id,
            "recorded_at": rec.recorded_at,
            "agent_id": rec.agent_id,
            "tool": rec.tool,
            "llm_model": rec.llm_model,
            "latency_ms": rec.latency_ms,
            "cost_usd": rec.cost_usd,
            "outcome": rec.outcome,
            "prompt_id": rec.prompt_id,
            "response_id": rec.response_id,
            "extra": rec.extra,
        }
        try:
            await col.update_one({"id": rec.id}, {"$setOnInsert": doc}, upsert=True)
        except Exception:
            logger.warning("AuditLogWriter: failed to persist audit record", exc_info=True)
            self._client = None  # force reconnect next time

    @classmethod
    def from_config(cls, config: Any) -> AuditLogWriter:
        """Build from a :class:`~caretaker.config.MaintainerConfig`.

        ``config.audit_log.enabled`` **and** ``config.mongo.enabled`` must
        both be true for MongoDB persistence to be active.
        """
        from caretaker.config import MaintainerConfig

        if not isinstance(config, MaintainerConfig):
            return cls(enabled=False)

        mongo_enabled = getattr(config, "mongo", None) is not None and config.mongo.enabled
        audit_enabled = getattr(config, "audit_log", None) is not None and config.audit_log.enabled
        enabled = mongo_enabled and audit_enabled

        mongodb_url_env = config.mongo.mongodb_url_env if mongo_enabled else "MONGODB_URL"
        database_name = config.mongo.database_name if mongo_enabled else "caretaker"
        audit_collection = config.mongo.audit_collection if mongo_enabled else "audit_log"
        return cls(
            enabled=enabled,
            mongodb_url_env=mongodb_url_env,
            database_name=database_name,
            collection_name=audit_collection,
        )


def _now_ms() -> int:
    """Return current time in milliseconds."""
    return int(time.monotonic() * 1000)
