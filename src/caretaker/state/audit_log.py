"""Audit log writer for agent decisions.

Writes structured audit records to:
- A Postgres ``audit_log`` table when ``audit_log.enabled = true`` and
  ``postgres.enabled = true`` (Phase 1 SaaS backend).
- A structured log line (JSON) in all cases for log-aggregation systems.

The Postgres table is created on first write (idempotent ``CREATE TABLE IF
NOT EXISTS``), so no separate migration is required for the writer to
function — though Alembic migrations should be run in production for proper
schema management.

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

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           UUID         PRIMARY KEY,
    run_id       TEXT         NOT NULL,
    recorded_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    agent_id     TEXT         NOT NULL,
    tool         TEXT,
    llm_model    TEXT,
    latency_ms   INTEGER,
    cost_usd     NUMERIC(12, 8),
    outcome      TEXT         NOT NULL,
    prompt_id    TEXT,
    response_id  TEXT,
    extra        JSONB
)
"""

_INSERT_SQL = """
INSERT INTO audit_log
    (id, run_id, recorded_at, agent_id, tool, llm_model, latency_ms, cost_usd,
     outcome, prompt_id, response_id, extra)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


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
    """Write audit records to Postgres and/or structured logging.

    Parameters
    ----------
    enabled:
        When ``False`` only structured-log output is produced; no Postgres
        writes are performed.
    database_url_env:
        Name of the environment variable containing the ``DATABASE_URL``.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        database_url_env: str = "DATABASE_URL",
    ) -> None:
        self._enabled = enabled
        self._database_url_env = database_url_env
        self._conn: "psycopg.AsyncConnection | None" = None  # type: ignore[type-arg]
        self._schema_created = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def _ensure_connection(self) -> "psycopg.AsyncConnection | None":  # type: ignore[type-arg]
        """Return a live async Postgres connection, or None if unavailable."""
        if not self._enabled:
            return None

        db_url = os.environ.get(self._database_url_env, "").strip()
        if not db_url:
            return None

        if self._conn is None or self._conn.closed:
            try:
                import psycopg

                # Neon / Supabase return "postgresql+psycopg://" URLs; strip the driver tag.
                clean_url = db_url.replace("postgresql+psycopg://", "postgresql://")
                self._conn = await psycopg.AsyncConnection.connect(
                    clean_url, autocommit=False
                )
                logger.debug("AuditLogWriter: connected to Postgres")
            except Exception:
                logger.warning(
                    "AuditLogWriter: failed to connect to Postgres; "
                    "audit records will not be persisted.",
                    exc_info=True,
                )
                return None

        return self._conn

    async def _ensure_schema(self, conn: "psycopg.AsyncConnection") -> None:  # type: ignore[type-arg]
        if self._schema_created:
            return
        try:
            await conn.execute(_CREATE_TABLE_SQL)
            await conn.commit()
            self._schema_created = True
            logger.debug("AuditLogWriter: ensured audit_log table")
        except Exception:
            await conn.rollback()
            logger.warning("AuditLogWriter: failed to create audit_log table", exc_info=True)

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

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

        Always emits a structured log line.  Also persists to Postgres when
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
        conn = await self._ensure_connection()
        if conn is None:
            return
        await self._ensure_schema(conn)
        try:
            extra_json = json.dumps(rec.extra) if rec.extra else None
            await conn.execute(
                _INSERT_SQL,
                (
                    rec.id,
                    rec.run_id,
                    rec.recorded_at,
                    rec.agent_id,
                    rec.tool,
                    rec.llm_model,
                    rec.latency_ms,
                    rec.cost_usd,
                    rec.outcome,
                    rec.prompt_id,
                    rec.response_id,
                    extra_json,
                ),
            )
            await conn.commit()
        except Exception:
            try:
                await conn.rollback()
            except Exception:
                pass
            logger.warning("AuditLogWriter: failed to persist audit record", exc_info=True)
            self._conn = None  # force reconnect next time

    @classmethod
    def from_config(cls, config: Any) -> "AuditLogWriter":
        """Build from a :class:`~caretaker.config.MaintainerConfig`.

        ``config.audit_log.enabled`` **and** ``config.postgres.enabled`` must
        both be true for Postgres persistence to be active.
        """
        from caretaker.config import MaintainerConfig

        if not isinstance(config, MaintainerConfig):
            return cls(enabled=False)

        pg_enabled = getattr(config, "postgres", None) is not None and config.postgres.enabled
        audit_enabled = (
            getattr(config, "audit_log", None) is not None and config.audit_log.enabled
        )
        enabled = pg_enabled and audit_enabled

        db_url_env = config.postgres.database_url_env if pg_enabled else "DATABASE_URL"
        return cls(enabled=enabled, database_url_env=db_url_env)


def _now_ms() -> int:
    """Return current time in milliseconds."""
    return int(time.monotonic() * 1000)
