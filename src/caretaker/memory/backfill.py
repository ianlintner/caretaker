"""Embedding backfill — Wave A3 companion to the fix ladder.

The write path (see :mod:`caretaker.memory.core`) stamps a
``summary_embedding`` on every new ``:AgentCoreMemory`` /
``:Incident`` row when ``write_embeddings`` is enabled. Existing
rows written before the toggle flipped don't have one, so Wave B3's
Neo4j-native vector-index retriever can't rank them. This module
walks those rows and populates the missing vector — one-shot,
idempotent, safe to re-run.

Design notes
------------

* Read + update only. No new nodes, no edge changes. The backfill
  uses :meth:`GraphStore.list_nodes_with_properties` to iterate
  candidates and :meth:`GraphStore.merge_node` to upsert the
  ``summary_embedding`` field in place. Retention / deletion are
  handled by the existing compaction job.
* Fail-closed on embedder errors. Any row whose embed call raises
  is left untouched so the next pass can retry; the summary counters
  surface the skip count to the operator.
* Hard bound on the window (default 30d). The old rows not covered
  by the window are intentionally skipped — the historical corpus
  ages out faster than the retriever needs it.
* Exposed through a synchronous helper (:func:`run_backfill_sync`)
  so the click command can call it without wiring its own event
  loop. The async core (:func:`run_backfill_async`) remains
  reusable for tests and for future in-process callers.

Usage:
    caretaker memory backfill-embeddings --since 30d
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from caretaker.graph.models import NodeType

if TYPE_CHECKING:
    from caretaker.memory.embeddings import Embedder

logger = logging.getLogger(__name__)

_DURATION_PATTERN = re.compile(r"^(?P<n>\d+)(?P<unit>[dhmw])$", re.IGNORECASE)
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30}

# Default labels to walk; :func:`run_backfill_async` accepts any
# subset so operators can backfill one label at a time.
_DEFAULT_LABELS = (NodeType.INCIDENT.value, NodeType.AGENT_CORE_MEMORY.value)


def _parse_since(since: str) -> timedelta:
    """Parse a ``30d`` / ``72h`` / ``2w`` window into a :class:`timedelta`.

    Raises :class:`ValueError` on malformed input so the click
    wrapper can surface a useful error to the operator.
    """
    match = _DURATION_PATTERN.match(since.strip())
    if not match:
        raise ValueError(f"Invalid --since value: {since!r}; expected e.g. '30d', '72h'")
    n = int(match.group("n"))
    unit = match.group("unit").lower()
    hours = _UNIT_HOURS.get(unit)
    if hours is None:
        raise ValueError(f"Invalid --since unit: {unit!r}")
    return timedelta(hours=n * hours)


class _BackfillStore(Protocol):
    """Subset of :class:`caretaker.graph.store.GraphStore` the backfill uses."""

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None: ...


async def run_backfill_async(
    *,
    store: _BackfillStore,
    embedder: Embedder | None,
    since: str = "30d",
    labels: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill embeddings on memory nodes missing a vector.

    Returns a summary dict: ``{"visited": int, "updated": int,
    "skipped_no_summary": int, "skipped_has_embedding": int,
    "errors": list[str]}``. Safe to call with
    ``embedder=None`` — the function reports every candidate as a
    skip and returns without touching the store.
    """
    labels = list(labels) if labels else list(_DEFAULT_LABELS)
    summary: dict[str, Any] = {
        "visited": 0,
        "updated": 0,
        "skipped_no_summary": 0,
        "skipped_has_embedding": 0,
        "skipped_no_embedder": 0,
        "errors": [],
        "dry_run": dry_run,
        "since": since,
        "labels": labels,
    }

    try:
        window = _parse_since(since)
    except ValueError as exc:
        summary["errors"].append(str(exc))
        return summary

    cutoff = (datetime.now(UTC) - window).isoformat()

    if embedder is None or not getattr(embedder, "available", False):
        # Still count the candidate rows — operators want to know
        # "how many would I have written if I wired an embedder?"
        summary["skipped_no_embedder"] = await _count_candidates(store, labels, cutoff)
        return summary

    for label in labels:
        rows = await store.list_nodes_with_properties(
            label,
            where="n.observed_at >= $cutoff",
            params={"cutoff": cutoff},
        )
        for row in rows:
            summary["visited"] += 1
            node_id = row.get("id")
            if not isinstance(node_id, str):
                # Nodes without a stable ``id`` can't be upserted.
                summary["errors"].append(f"{label}: row missing id")
                continue
            existing_vec = row.get("summary_embedding")
            if existing_vec:
                summary["skipped_has_embedding"] += 1
                continue
            summary_text = (row.get("summary") or "").strip()
            if not summary_text:
                summary["skipped_no_summary"] += 1
                continue
            try:
                vector = await embedder.embed(summary_text)
            except Exception as exc:  # noqa: BLE001 - fail-closed on embed errors
                logger.info("backfill: embed failed for %s/%s: %s", label, node_id, exc)
                summary["errors"].append(f"{label}:{node_id}: embed failed")
                continue
            if not vector:
                summary["skipped_no_summary"] += 1
                continue
            if dry_run:
                summary["updated"] += 1
                continue
            props = {
                "summary_embedding": ",".join(f"{float(v):.6f}" for v in vector),
            }
            try:
                await store.merge_node(label, node_id, props)
            except Exception as exc:  # noqa: BLE001 - write failure should not abort the pass
                logger.info("backfill: merge_node failed for %s/%s: %s", label, node_id, exc)
                summary["errors"].append(f"{label}:{node_id}: merge failed")
                continue
            summary["updated"] += 1

    return summary


async def _count_candidates(store: _BackfillStore, labels: list[str], cutoff: str) -> int:
    total = 0
    for label in labels:
        rows = await store.list_nodes_with_properties(
            label,
            where="n.observed_at >= $cutoff",
            params={"cutoff": cutoff},
        )
        total += len(rows)
    return total


def run_backfill_sync(
    *,
    since: str = "30d",
    config_path: str | None = None,
    dry_run: bool = False,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper that wires config → store + embedder.

    Kept out of :mod:`caretaker.cli` so the click command stays
    focused on argument parsing. The function constructs the Neo4j
    :class:`~caretaker.graph.store.GraphStore` and the configured
    :class:`~caretaker.memory.embeddings.Embedder` itself — callers
    in tests should prefer :func:`run_backfill_async` and pass fakes.
    """
    return asyncio.run(_run_backfill_with_config(since, config_path, dry_run, labels))


async def _run_backfill_with_config(
    since: str,
    config_path: str | None,
    dry_run: bool,
    labels: list[str] | None,
) -> dict[str, Any]:
    from caretaker.config import MaintainerConfig
    from caretaker.memory.embeddings import LiteLLMEmbedder

    # Config is optional — if not supplied we rely on env-driven
    # Neo4j and default embedder settings.
    llm_config = None
    if config_path is not None:
        try:
            maintainer = MaintainerConfig.from_yaml(config_path)
        except Exception as exc:  # noqa: BLE001 - surface to CLI via error list
            return {
                "visited": 0,
                "updated": 0,
                "errors": [f"config load failed: {exc}"],
                "since": since,
                "dry_run": dry_run,
            }
        llm_config = getattr(maintainer, "llm", None)

    embedder: Embedder | None
    try:
        if llm_config is not None:
            embedder = LiteLLMEmbedder.from_config(llm_config)
        else:
            embedder = LiteLLMEmbedder()
    except Exception as exc:  # noqa: BLE001 - no embedder → dry summary
        logger.info("backfill: embedder construction failed (%s)", exc)
        embedder = None

    try:
        from caretaker.graph.store import GraphStore

        store = GraphStore()
    except Exception as exc:  # noqa: BLE001 - Neo4j unavailable → surface error
        return {
            "visited": 0,
            "updated": 0,
            "errors": [f"graph store unavailable: {exc}"],
            "since": since,
            "dry_run": dry_run,
        }

    try:
        return await run_backfill_async(
            store=store,
            embedder=embedder,
            since=since,
            labels=labels,
            dry_run=dry_run,
        )
    finally:
        with contextlib.suppress(Exception):
            await store.close()


__all__ = [
    "run_backfill_async",
    "run_backfill_sync",
]
