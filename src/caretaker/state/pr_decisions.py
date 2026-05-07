"""Per-PR decision-timeline writer.

Companion to :mod:`caretaker.state.audit_log` but scoped to one
``(owner, repo, pr_number)`` slug. Every decision the PR-reviewer
pipeline makes (review_started, tier_classified, auto_fix_dispatched,
…) is appended here so the admin SPA can render a chronological
timeline for any PR without `kubectl logs | grep`.

Storage
-------
Writes to a MongoDB ``pr_decisions`` collection when MongoDB is
enabled. Indexes:

* ``(repo, pr_number, observed_at)`` — efficient timeline query.
* TTL on ``observed_at`` (30 days) — keeps the collection bounded.

Both indexes are created idempotently on first write.

Each record carries the OTel ``trace_id`` + ``span_id`` of the active
span (when one exists) so the admin UI can deep-link from a timeline
row into the corresponding Tempo trace.

Reliability
-----------
Mongo failures NEVER propagate to the agent. Errors are logged at
WARNING and the helper returns. A structured INFO log line is also
emitted for every record so log-aggregation consumers (Loki) can
build the timeline from logs even if Mongo is down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from caretaker.observability.tracer_compat import get_current_span

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import motor.motor_asyncio

# Bounded, mypy-strict-checked vocabularies for the agent + event
# columns. Free-text strings would silently drift over time
# (``auto_fix_complete`` vs ``auto_fix_completed`` vs typos), splitting
# the timeline into orphaned events that the admin SPA can't group.
DecisionAgent = Literal[
    "pr_reviewer",
    "pr_agent",
    "auto_fix",
    "complexity_classifier",
    "opencode_local",
]
DecisionEvent = Literal[
    "review_started",
    "review_skipped",
    "review_completed",
    "tier_classified",
    "auto_fix_dispatched",
    "auto_fix_complete",
    "auto_fix_skipped",
    "opencode_review_started",
    "opencode_review_finished",
    "opencode_review_failed",
    "inline_review_started",
    "inline_review_finished",
]

# Module-level singleton, lazily initialised by ``configure_default_store``.
_default_store: PRDecisionStore | None = None


def configure_default_store(store: PRDecisionStore | None) -> None:
    """Install (or clear) the process-wide default store.

    Called at app startup once the config is loaded so call sites can
    use the :func:`record_decision` shortcut without plumbing the store
    through every layer.

    If a previous store was installed and the new value is a different
    instance (or ``None``), the old store's Mongo client is scheduled
    for cleanup on the running event loop. This prevents leaking a
    Motor client when the function is called multiple times during
    hot-reload / test-suite teardown.
    """
    global _default_store  # noqa: PLW0603 — process singleton.
    previous = _default_store
    _default_store = store
    if previous is not None and previous is not store:
        # Best-effort async close. If no loop is running (e.g. sync
        # test harness) we just drop the reference — the underlying
        # Motor client will be GC'd by the interpreter.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(previous.close())


def get_default_store() -> PRDecisionStore | None:
    """Return the process-wide default store, or ``None`` if unset."""
    return _default_store


def _capture_trace_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id_hex, span_id_hex)`` for the active span, if any.

    Hex format matches what OTel collectors emit so admin clients can
    feed it directly into Tempo / Jaeger lookup URLs. Returns
    ``(None, None)`` whenever there's no active span (or the OTel SDK
    is the no-op fallback).
    """
    try:
        span = get_current_span()
        if span is None:
            return None, None
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return None, None
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:  # pragma: no cover - defensive
        return None, None


class PRDecisionStore:
    """Append-only per-PR decision log writer."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        mongodb_url_env: str = "MONGODB_URL",
        database_name: str = "caretaker",
        collection_name: str = "pr_decisions",
        ttl_seconds: int = 30 * 86400,
    ) -> None:
        self._enabled = enabled
        self._mongodb_url_env = mongodb_url_env
        self._database_name = database_name
        self._collection_name = collection_name
        self._ttl_seconds = ttl_seconds
        self._client: motor.motor_asyncio.AsyncIOMotorClient[Any] | None = None
        self._indexes_created = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def _ensure_collection(
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
                logger.debug("PRDecisionStore: connected to MongoDB")
            except Exception:
                logger.warning(
                    "PRDecisionStore: failed to connect to MongoDB; "
                    "decision records will not be persisted.",
                    exc_info=True,
                )
                return None

        col = self._client[self._database_name][self._collection_name]
        if not self._indexes_created:
            with contextlib.suppress(Exception):
                import pymongo

                # Compound index for query_timeline lookups.
                await col.create_index(
                    [
                        ("repo", pymongo.ASCENDING),
                        ("pr_number", pymongo.ASCENDING),
                        ("observed_at", pymongo.ASCENDING),
                    ],
                    name="idx_repo_pr_observed",
                )
                # TTL index — Mongo will purge documents after ``ttl_seconds``.
                await col.create_index(
                    "observed_at",
                    expireAfterSeconds=self._ttl_seconds,
                    name="idx_observed_at_ttl",
                )
                self._indexes_created = True
        return col

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        self._client = None

    # ── Public API ─────────────────────────────────────────────────────

    async def record(
        self,
        *,
        repo: str,
        pr_number: int,
        agent: DecisionAgent,
        event: DecisionEvent,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Persist one decision record + emit a structured log line.

        Mongo writes never raise — failures are logged at WARNING and
        the call returns normally so observability never fails the
        agent run.
        """
        trace_id, span_id = _capture_trace_ids()
        doc: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "repo": repo,
            "pr_number": int(pr_number),
            "observed_at": datetime.now(UTC),
            "agent": agent,
            "event": event,
            "fields": fields or {},
            "trace_id": trace_id,
            "span_id": span_id,
        }

        # Always emit the structured log line — even when Mongo is
        # unreachable Loki / stdout can still rebuild the timeline.
        self._emit_log(doc)

        col = await self._ensure_collection()
        if col is None:
            return
        try:
            await col.insert_one(doc)
        except Exception:
            logger.warning(
                "PRDecisionStore: failed to persist decision %s for %s#%d",
                event,
                repo,
                pr_number,
                exc_info=True,
            )
            self._client = None  # force reconnect next time

    async def query_timeline(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(decisions, total_count)`` for one PR.

        ``decisions`` are sorted by ``observed_at`` ascending (oldest
        first), bounded by ``limit`` and skipped past ``offset`` so the
        admin SPA can paginate. ``total_count`` is the unfiltered count
        of matching documents so the SPA can render a "showing N of M"
        affordance and detect truncation.

        Returns ``([], 0)`` when Mongo is unreachable or the PR has no
        records.
        """
        col = await self._ensure_collection()
        if col is None:
            return [], 0
        repo_slug = f"{owner}/{repo}"
        query = {"repo": repo_slug, "pr_number": int(pr_number)}
        try:
            total_count = int(await col.count_documents(query))
            cursor = col.find(query).sort("observed_at", 1).skip(int(offset)).limit(int(limit))
            docs: list[dict[str, Any]] = []
            async for doc in cursor:
                # Drop Mongo's internal ``_id`` so the response is JSON-clean.
                doc.pop("_id", None)
                docs.append(doc)
            return docs, total_count
        except Exception:
            logger.warning(
                "PRDecisionStore: failed to query timeline for %s#%d",
                repo_slug,
                pr_number,
                exc_info=True,
            )
            return [], 0

    # ── Internals ──────────────────────────────────────────────────────

    def _emit_log(self, doc: dict[str, Any]) -> None:
        logger.info(
            "pr_decision repo=%s pr=%d agent=%s event=%s",
            doc["repo"],
            doc["pr_number"],
            doc["agent"],
            doc["event"],
            extra={
                "audit_event": "pr_decision",
                "id": doc["id"],
                "repo": doc["repo"],
                "pr_number": doc["pr_number"],
                "agent": doc["agent"],
                "event": doc["event"],
                "fields": doc["fields"],
                "trace_id": doc["trace_id"],
                "span_id": doc["span_id"],
            },
        )

    @classmethod
    def from_config(cls, config: Any) -> PRDecisionStore:
        """Build from a :class:`~caretaker.config.MaintainerConfig`.

        Reuses the same ``mongo`` block as the audit-log writer. Enable
        precedence (highest first):

        1. ``config.mongo.enabled`` if explicitly set in the loaded
           config (truthy or falsy) — explicit configuration always wins.
        2. Env-var fallback: enabled when ``MONGODB_URL`` (or the
           configured ``mongodb_url_env``) is non-empty in the
           environment. This catches the deployed-backend case where no
           ``config.yml`` is mounted but ``MONGODB_URL`` is set in the
           pod env (same one ``audit_log`` and ``fleet_clients`` use).
        3. Disabled — falls back to structured-log-only mode; ``record``
           still emits an INFO line so Loki can rebuild the timeline.
        """
        from caretaker.config import MaintainerConfig

        if not isinstance(config, MaintainerConfig):
            return cls(enabled=False)

        mongo_block = getattr(config, "mongo", None)
        mongodb_url_env = mongo_block.mongodb_url_env if mongo_block is not None else "MONGODB_URL"
        database_name = mongo_block.database_name if mongo_block is not None else "caretaker"

        # Determine enable state. Pydantic's ``model_fields_set`` lets us
        # tell "user explicitly set enabled=False" from "user never
        # touched this field". When unset, fall back to detecting the
        # env-var so backend pods (which don't mount a config.yml) still
        # persist decisions instead of silently log-only.
        if mongo_block is not None and "enabled" in getattr(mongo_block, "model_fields_set", set()):
            enabled = bool(mongo_block.enabled)
        else:
            enabled = bool(os.environ.get(mongodb_url_env, "").strip())

        return cls(
            enabled=enabled,
            mongodb_url_env=mongodb_url_env,
            database_name=database_name,
            collection_name="pr_decisions",
        )


async def record_decision(
    repo: str,
    pr_number: int,
    agent: DecisionAgent,
    event: DecisionEvent,
    **fields: Any,
) -> None:
    """Module-level shortcut: write one decision via the default store.

    Lazily falls back to a disabled store (structured-log only) when
    nobody called :func:`configure_default_store` so call sites can
    ``await`` without checking config first. ``store.record`` already
    swallows Mongo failures and the trace-id capture has its own
    guard, so this wrapper deliberately does not add another
    ``try/except`` — nothing inside should raise.
    """
    store = _default_store
    if store is None:
        store = PRDecisionStore(enabled=False)
    await store.record(
        repo=repo,
        pr_number=pr_number,
        agent=agent,
        event=event,
        fields=dict(fields),
    )


__all__ = [
    "DecisionAgent",
    "DecisionEvent",
    "PRDecisionStore",
    "configure_default_store",
    "get_default_store",
    "record_decision",
]
