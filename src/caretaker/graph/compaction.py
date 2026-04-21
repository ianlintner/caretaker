"""Tiered compaction + salience scoring — Milestone M4 of the memory-graph plan.

The per-tenant Neo4j graph grows without bound in its tier-0 form (raw
``:Run`` / ``:AuditEvent`` / ``:CausalEvent`` rows). M4 adds the
forgetting policy called out in ``docs/memory-graph-plan.md`` §4:

* **Tier 0 → Tier 1** — a nightly rollup collapses every ``:Run`` in a
  given ISO week into a single ``:RunSummaryWeek{repo, week_of, ...}``
  node. The week node carries aggregated counters so the admin
  dashboard can keep rendering weekly telemetry after the raw rows
  have been evicted.
* **Tier 1 → Tier 2** — skills already persisted in ``InsightStore``
  are the durable procedural distillation. This module does not
  promote skills itself; it only ships the ``LEARNED_IN`` edge type
  (see :class:`caretaker.graph.models.RelType`) so the crystalliser
  can link a skill back to the week it was learned in.
* **Pruning** — after the rollup has landed, runs / causal events /
  audit events older than ``cutoff_days`` are deleted when their
  salience score falls below the per-tenant threshold. Any node
  carrying ``pinned: true`` is preserved unconditionally, and
  ``:Goal`` + ``:Skill`` nodes are never candidates for deletion
  (safety over completeness — ``docs/memory-graph-plan.md`` §4.1
  lists these as immutable exemptions).

Salience scoring (:func:`compute_salience`) is pure stdlib — the
formula is the weighted sum in ``docs/memory-graph-plan.md`` §4.2:

    salience = 0.3 * escalation_count
             + 0.3 * unexpected_outcome
             + 0.2 * recency_decay
             + 0.2 * connectivity

Each component is normalised to ``[0, 1]`` before weighting so the
total is also bounded there, making the threshold configuration
tenant-portable.

Contract for call sites: :func:`run_nightly` is best-effort. It is
invoked on a 24-hour heartbeat from the admin refresh loop and from
the ``POST /api/admin/graph/compact`` endpoint. Both paths swallow
exceptions — compaction drift is auto-healed on the next tick, and we
never want a slow Neo4j to wedge the orchestrator.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from caretaker.graph.models import GraphNode, NodeType

if TYPE_CHECKING:
    from caretaker.state.models import RunSummary

logger = logging.getLogger(__name__)


# ── Store surface ────────────────────────────────────────────────────────────
#
# The compaction routines take a :class:`GraphStore`-shaped object rather
# than the concrete class so unit tests can pass a fake that records the
# mutations. Only the subset actually used here is in the protocol — this
# keeps the surface narrow and the test fake small.


class _SupportsCompaction(Protocol):
    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None: ...

    async def list_nodes_with_properties(
        self,
        label: str,
        *,
        where: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def delete_node(self, label: str, node_id: str) -> None: ...


# ── Salience scoring ────────────────────────────────────────────────────────

# The weights below are load-bearing: the task spec requires them to sum
# to ``1.0`` so the resulting salience lies in ``[0, 1]`` whenever every
# component is itself normalised to ``[0, 1]``. Keep in sync with the
# prose in ``docs/memory-graph-plan.md`` §4.2.
_W_ESCALATION = 0.3
_W_UNEXPECTED = 0.3
_W_RECENCY = 0.2
_W_CONNECTIVITY = 0.2

# Normalisation caps — picked so "a full run's worth of escalations /
# errors / neighbours" maps onto 1.0. The exact values are heuristics,
# not a contract; they only matter insofar as they give
# ``compute_salience`` a monotone, bounded output.
_ESCALATION_NORM_CAP = 10.0
_CONNECTIVITY_NORM_CAP = 20.0
_RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass(frozen=True)
class _SalienceInputs:
    """Normalised components of :func:`compute_salience`.

    Exposed via the dataclass so tests / operators can introspect
    intermediate values without re-deriving them from the weighted sum.
    """

    escalation_count: float
    unexpected_outcome: float
    recency_decay: float
    connectivity: float


def _normalise(value: float, cap: float) -> float:
    """Clamp ``value / cap`` to ``[0, 1]``. Zero-cap falls back to zero."""
    if cap <= 0 or not math.isfinite(value):
        return 0.0
    ratio = value / cap
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio


def _age_days(run_at: datetime | None, *, now: datetime | None = None) -> float:
    """Age in days between ``run_at`` and ``now`` (``UTC`` fallback).

    Naive datetimes are treated as UTC — the orchestrator historically
    emits a mix of aware / naive timestamps (see the 2026-W17 refactor
    in ``state/models.py``) and compaction must not crash on the older
    rows.
    """
    if run_at is None:
        return float("inf")
    reference = now or datetime.now(UTC)
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    delta = reference - run_at
    return max(delta.total_seconds() / 86400.0, 0.0)


def _run_escalation_count(run: Any) -> int:
    """Sum every per-agent escalation counter on a ``RunSummary`` duck.

    The runtime type is :class:`caretaker.state.models.RunSummary` but
    callers occasionally pass a dict loaded from Neo4j (``list_nodes_
    with_properties`` returns raw property bags). We treat both
    uniformly via ``getattr`` / ``dict.get`` so the scorer works
    against either shape without the caller having to rehydrate the
    pydantic model.
    """
    fields = (
        "prs_escalated",
        "issues_escalated",
        "stale_assignments_escalated",
        "goal_escalation_count",
        "escalation_items_found",
    )
    total = 0
    for name in fields:
        value = _get(run, name, 0)
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def _run_unexpected_outcome(run: Any) -> float:
    """Score ``run``'s "did something surprising happen?" in ``[0, 1]``.

    Signals (highest-weight first):

    * ``errors`` list is non-empty → 1.0 (hard failure recorded).
    * ``escalation_rate`` ≥ 0.5 → 1.0 (majority of items escalated).
    * otherwise we fall back to the escalation-rate verbatim since it's
      already a ratio.
    """
    errors = _get(run, "errors", None)
    if errors:
        # ``errors`` may arrive as a list or a JSON-encoded string when
        # rehydrated from Neo4j; both non-empty cases count as hard.
        try:
            if len(errors) > 0:
                return 1.0
        except TypeError:
            return 1.0
    rate = _get(run, "escalation_rate", 0.0) or 0.0
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        return 0.0
    if rate_f >= 0.5:
        return 1.0
    if rate_f < 0.0:
        return 0.0
    return rate_f


def _get(obj: Any, name: str, default: Any) -> Any:
    """``getattr`` → ``dict.get`` fallback so RunSummary + dicts both work."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def compute_salience(
    run: RunSummary | dict[str, Any] | Any,
    *,
    connectivity: int | None = None,
    now: datetime | None = None,
) -> float:
    """Return the salience of ``run`` in ``[0, 1]``.

    ``run`` may be a :class:`RunSummary`, a plain dict (as returned by
    ``GraphStore.list_nodes_with_properties``), or any object exposing
    the same attribute names. ``connectivity`` is the degree of the
    corresponding ``:Run`` / ``:CausalEvent`` node in the causal-chain
    subgraph; when the caller does not have it we read ``connectivity``
    off the object itself (defaulting to zero — unknown degree is
    treated as low salience, which is conservative for pruning).

    The individual components are each normalised to ``[0, 1]`` so that
    the weighted sum stays bounded. See module docstring for the
    weights.
    """
    inputs = _SalienceInputs(
        escalation_count=_normalise(_run_escalation_count(run), _ESCALATION_NORM_CAP),
        unexpected_outcome=max(0.0, min(1.0, _run_unexpected_outcome(run))),
        recency_decay=math.exp(
            -_age_days(_coerce_datetime(_get(run, "run_at", None)), now=now)
            / _RECENCY_HALF_LIFE_DAYS
        ),
        connectivity=_normalise(
            float(connectivity if connectivity is not None else _get(run, "connectivity", 0) or 0),
            _CONNECTIVITY_NORM_CAP,
        ),
    )
    salience = (
        _W_ESCALATION * inputs.escalation_count
        + _W_UNEXPECTED * inputs.unexpected_outcome
        + _W_RECENCY * inputs.recency_decay
        + _W_CONNECTIVITY * inputs.connectivity
    )
    # Floating-point drift from the weighted sum can push the value a
    # hair outside ``[0, 1]``; clamp so the contract with the threshold
    # check in :func:`prune_low_salience` is watertight.
    if salience < 0.0:
        return 0.0
    if salience > 1.0:
        return 1.0
    return salience


def _coerce_datetime(value: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 string or pass-through datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# ── Tier-0 → Tier-1 rollup ──────────────────────────────────────────────────


def _iso_week_bounds(week_of: date) -> tuple[datetime, datetime, str]:
    """Return ``[start, end)`` UTC datetimes + canonical ``YYYY-WNN`` key.

    ``week_of`` is normalised to the Monday at the start of its ISO week
    so callers can pass any day and still land on the same bucket.
    """
    iso_year, iso_week, _ = week_of.isocalendar()
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    end = start + timedelta(days=7)
    key = f"{iso_year:04d}-W{iso_week:02d}"
    return start, end, key


async def compact_tier0_to_tier1(
    store: _SupportsCompaction,
    repo: str,
    week_of: date,
) -> GraphNode:
    """Merge a ``:RunSummaryWeek`` rollup for ``repo`` and the ISO week of ``week_of``.

    Scans every ``:Run{repo=$repo}`` node whose ``run_at`` falls inside
    the ISO week containing ``week_of`` and aggregates them into a
    single tier-1 node carrying:

    * ``run_count`` — number of runs rolled up.
    * ``prs_merged_total`` / ``issues_triaged_total`` — summed
      counters.
    * ``escalation_rate_mean`` — arithmetic mean of per-run escalation
      rates (``0.0`` for an empty bucket).
    * ``top_skill_ids`` — kept as an empty list here so the upstream
      skill-crystalliser can populate it when it promotes a weekly
      skill via the new ``LEARNED_IN`` edge. Kept as a property (not
      an edge) because an empty list is cheaper to write than a round
      trip through the driver.

    Returns the merged :class:`GraphNode` so call sites can log or
    surface it via the admin API.
    """
    start, end, key = _iso_week_bounds(week_of)

    runs = await store.list_nodes_with_properties(
        NodeType.RUN.value,
        where=(
            "n.repo = $repo AND n.run_at >= $start AND n.run_at < $end "
            "AND n.run_at IS NOT NULL AND n.run_at <> ''"
        ),
        params={
            "repo": repo,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )

    run_count = len(runs)
    prs_merged_total = 0
    issues_triaged_total = 0
    escalation_rates: list[float] = []
    for row in runs:
        prs_merged_total += int(row.get("prs_merged") or 0)
        issues_triaged_total += int(row.get("issues_triaged") or 0)
        rate = row.get("escalation_rate")
        if rate is None:
            continue
        try:
            escalation_rates.append(float(rate))
        except (TypeError, ValueError):
            continue

    escalation_rate_mean = (
        sum(escalation_rates) / len(escalation_rates) if escalation_rates else 0.0
    )

    node_id = f"runweek:{repo}:{key}"
    properties: dict[str, Any] = {
        "repo": repo,
        "week_of": start.date().isoformat(),
        "week_key": key,
        "run_count": run_count,
        "prs_merged_total": prs_merged_total,
        "issues_triaged_total": issues_triaged_total,
        "escalation_rate_mean": escalation_rate_mean,
        "top_skill_ids": [],
        "rolled_up_at": datetime.now(UTC).isoformat(),
    }
    await store.merge_node(NodeType.RUN_SUMMARY_WEEK.value, node_id, properties)

    return GraphNode(
        id=node_id,
        type=NodeType.RUN_SUMMARY_WEEK.value,
        label=f"{repo} {key}",
        properties=properties,
    )


# ── Pruning pass ────────────────────────────────────────────────────────────


# Labels that are candidates for salience-based pruning. Kept as a
# module-level constant so the "never delete Goal / Skill / Repo /
# RunSummaryWeek" invariant is discoverable — if you add a label
# here, update the safety doc in ``docs/memory-graph-plan.md`` §4.1.
_PRUNABLE_LABELS: tuple[str, ...] = (
    NodeType.RUN.value,
    NodeType.AUDIT_EVENT.value,
    NodeType.CAUSAL_EVENT.value,
)


async def prune_low_salience(
    store: _SupportsCompaction,
    repo: str,
    *,
    threshold: float = 0.25,
    cutoff_days: int = 30,
    now: datetime | None = None,
) -> int:
    """Delete low-salience tier-0 nodes older than ``cutoff_days``.

    Walks ``:Run``, ``:AuditEvent``, and ``:CausalEvent`` scoped to
    ``repo``; computes each node's salience via :func:`compute_salience`
    and deletes it when:

    1. ``run_at`` / ``observed_at`` is older than ``cutoff_days``; and
    2. salience is strictly below ``threshold``; and
    3. the node is not ``pinned: true``.

    ``:Goal`` and ``:Skill`` nodes are **never** deleted — they are the
    durable semantic / procedural tier and are protected at the label
    level rather than via a per-node pin. Returns the total count of
    nodes actually deleted.
    """
    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(days=cutoff_days)
    deleted_total = 0

    for label in _PRUNABLE_LABELS:
        rows = await store.list_nodes_with_properties(
            label,
            where="n.repo = $repo",
            params={"repo": repo},
        )
        for row in rows:
            # Safety gate 1: explicit pin. Stored as a boolean in Neo4j
            # but may arrive as the string "true" from some older
            # writers — normalise before the check.
            if _is_pinned(row.get("pinned")):
                continue

            # Safety gate 2: age. The timestamp we read off the node
            # depends on the label — runs use ``run_at``, audit and
            # causal events use ``observed_at``. Fall back across both
            # so a partially-stamped node doesn't slip through.
            timestamp = _coerce_datetime(row.get("run_at") or row.get("observed_at"))
            if timestamp is None:
                # No timestamp means we can't reason about age — skip
                # rather than risk deleting a load-bearing node.
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp > cutoff:
                continue

            salience = compute_salience(row, now=reference)
            if salience >= threshold:
                continue

            node_id = row.get("id")
            if not isinstance(node_id, str) or not node_id:
                continue
            try:
                await store.delete_node(label, node_id)
            except Exception:  # store errors are best-effort
                logger.warning(
                    "prune_low_salience: failed to delete %s %r", label, node_id, exc_info=True
                )
                continue
            deleted_total += 1

    return deleted_total


def _is_pinned(value: Any) -> bool:
    """Normalise the ``pinned`` property across bool / str / int shapes."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, int):
        return value != 0
    return False


# ── Nightly orchestrator entry point ─────────────────────────────────────────


async def run_nightly(
    store: _SupportsCompaction,
    repo: str,
    *,
    week_of: date | None = None,
    threshold: float = 0.25,
    cutoff_days: int = 30,
    now: datetime | None = None,
) -> dict[str, int]:
    """One-shot compaction job: tier-0 → tier-1 rollup, then prune.

    Runs the ISO-week rollup for the week containing ``week_of``
    (default: last week, i.e. ``today - 7d``) and then the
    :func:`prune_low_salience` pass. Returns a counter dict:

    * ``rolled_up_runs``: how many ``:Run`` rows fed the rollup.
    * ``pruned``: how many tier-0 nodes were deleted.

    Exceptions from either phase are logged and swallowed — compaction
    is best-effort by design, per ``docs/memory-graph-plan.md`` §10.
    """
    reference = now or datetime.now(UTC)
    target_week = week_of or (reference.date() - timedelta(days=7))

    rolled_up_runs = 0
    try:
        week_node = await compact_tier0_to_tier1(store, repo, target_week)
        run_count_raw = week_node.properties.get("run_count", 0)
        if isinstance(run_count_raw, int):
            rolled_up_runs = run_count_raw
        elif isinstance(run_count_raw, (str, float)):
            try:
                rolled_up_runs = int(run_count_raw)
            except (TypeError, ValueError):
                rolled_up_runs = 0
    except Exception:
        logger.warning("compact_tier0_to_tier1 failed for %s", repo, exc_info=True)

    pruned = 0
    try:
        pruned = await prune_low_salience(
            store,
            repo,
            threshold=threshold,
            cutoff_days=cutoff_days,
            now=reference,
        )
    except Exception:
        logger.warning("prune_low_salience failed for %s", repo, exc_info=True)

    return {"rolled_up_runs": rolled_up_runs, "pruned": pruned}


__all__ = [
    "compact_tier0_to_tier1",
    "compute_salience",
    "prune_low_salience",
    "run_nightly",
]
