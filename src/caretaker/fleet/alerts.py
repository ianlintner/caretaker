""":FleetAlert evaluator + in-memory store (Phase 3 §4.4, T-E4).

Fleet heartbeats (see :class:`caretaker.fleet.emitter.FleetHeartbeat`) carry
``goal_health`` and ``error_count`` for every consumer repo. Today the admin
UI renders them straight through. This module adds a thin alerting path on
top: when a repo trips one of the four configured alert conditions, an
idempotent :class:`FleetAlert` row is opened (and a ``:FleetAlert`` graph
node is upserted). Once the underlying metric clears, the alert is
auto-resolved — ``resolved_at`` is set and the admin ``open=true`` listing
omits it.

Four alert kinds:

* ``goal_health_regression`` — last N heartbeats from a repo all below
  ``goal_health_threshold``.
* ``error_spike`` — most recent heartbeat's ``error_count`` is at least
  ``error_spike_multiplier`` times the mean of the prior heartbeats'
  ``error_count`` (with a ``mean ≥ 1`` floor so a jump from 0 → 3 still
  trips).
* ``ghosted`` — no heartbeat from the repo for ``ghosted_window_days`` (the
  repo is known to the registry but has gone dark).
* ``scope_gap`` — piggybacks on the existing scope-gap tracker via the
  heartbeat ``summary``: when ``summary.scope_gap_open`` is truthy the
  evaluator opens a matching FleetAlert, dedup on ``(repo, kind)``.

Design notes
------------

* :func:`evaluate_fleet_alerts` is a pure function over an iterable of
  :class:`~caretaker.fleet.emitter.FleetHeartbeat` rows. The store-side
  history ring buffer lives in :class:`caretaker.fleet.store.FleetRegistryStore`;
  this module never reads from it directly so the evaluator stays trivially
  testable.
* :class:`FleetAlertStore` dedups on ``(repo, kind)`` so calling the
  evaluator twice on the same data doesn't double-emit. Resolution runs as
  part of the same upsert pass: a repo that was alerting but now has a
  heartbeat back above threshold flips ``resolved_at`` instead of
  re-opening.
* :func:`upsert_fleet_alerts` writes a ``:FleetAlert`` node per alert with
  ``repo``, ``kind``, ``severity``, ``summary``, ``opened_at``, and
  ``resolved_at``. Graph writes are best-effort; the admin API does not
  block on graph availability.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from caretaker.fleet.emitter import FleetHeartbeat
from caretaker.graph.models import NodeType

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


AlertKind = Literal[
    "goal_health_regression",
    "error_spike",
    "ghosted",
    "scope_gap",
]

AlertSeverity = Literal["warning", "critical"]


class FleetAlert(BaseModel):
    """One open (or recently-resolved) alert for a repo + kind pair.

    ``(repo, kind)`` is the dedup key — at most one row per pair at a time.
    ``resolved_at`` is ``None`` while the alert is active and becomes a
    timestamp when the next evaluation pass sees the metric clear.
    """

    repo: str
    kind: AlertKind
    severity: AlertSeverity
    # 240 chars is plenty for the admin list row; longer context belongs in
    # ``details``. Also guards against a runaway summary in the graph node
    # properties blob.
    summary: str = Field(max_length=240)
    opened_at: datetime
    resolved_at: datetime | None = None
    details: dict[str, object] = Field(default_factory=dict)

    def is_open(self) -> bool:
        return self.resolved_at is None


# ── Evaluation ───────────────────────────────────────────────────────────


def _coerce_heartbeat(value: FleetHeartbeat | dict[str, Any]) -> FleetHeartbeat:
    """Accept either a :class:`FleetHeartbeat` or the raw dict stored by
    :meth:`FleetRegistryStore.recent_heartbeats`.

    Keeps the public evaluator API typed as ``list[FleetHeartbeat]`` while
    the store's ring buffer (dicts) can feed it directly without the caller
    hand-rolling ``FleetHeartbeat(**row)`` everywhere.
    """
    if isinstance(value, FleetHeartbeat):
        return value
    # Drop summary if it's ``None`` — pydantic accepts either, but omitting
    # it keeps the shape clean.
    payload = dict(value)
    return FleetHeartbeat.model_validate(payload)


def _group_by_repo(
    heartbeats: Iterable[FleetHeartbeat | dict[str, Any]],
) -> dict[str, list[FleetHeartbeat]]:
    grouped: dict[str, list[FleetHeartbeat]] = {}
    for raw in heartbeats:
        try:
            hb = _coerce_heartbeat(raw)
        except Exception:
            logger.debug("evaluate_fleet_alerts: skipping malformed heartbeat", exc_info=True)
            continue
        grouped.setdefault(hb.repo, []).append(hb)
    for repo, rows in grouped.items():
        rows.sort(key=lambda h: h.run_at)
        grouped[repo] = rows
    return grouped


def _goal_health_alert(
    repo: str,
    rows: list[FleetHeartbeat],
    *,
    threshold: float,
    n_consecutive: int,
    now: datetime,
) -> FleetAlert | None:
    if len(rows) < n_consecutive or n_consecutive < 1:
        return None
    tail = rows[-n_consecutive:]
    # ``None`` goal_health doesn't count as a failing sample — without the
    # metric we can't make a claim either way.
    scores = [row.goal_health for row in tail if row.goal_health is not None]
    if len(scores) < n_consecutive:
        return None
    if any(score >= threshold for score in scores):
        return None
    opened_at = tail[0].run_at
    severity: AlertSeverity = "critical" if scores[-1] < threshold / 2 else "warning"
    return FleetAlert(
        repo=repo,
        kind="goal_health_regression",
        severity=severity,
        summary=(
            f"goal_health below {threshold:.2f} for last "
            f"{n_consecutive} heartbeats (latest={scores[-1]:.2f})"
        ),
        opened_at=opened_at,
        details={
            "threshold": threshold,
            "n_consecutive": n_consecutive,
            "samples": scores,
        },
    )


def _error_spike_alert(
    repo: str,
    rows: list[FleetHeartbeat],
    *,
    multiplier: float,
    now: datetime,
) -> FleetAlert | None:
    if len(rows) < 2:
        return None
    prior = rows[:-1]
    latest = rows[-1]
    prior_counts = [int(r.error_count or 0) for r in prior]
    mean_prior = max(sum(prior_counts) / len(prior_counts), 1.0)
    threshold = mean_prior * multiplier
    if latest.error_count < threshold:
        return None
    severity: AlertSeverity = "critical" if latest.error_count >= threshold * 2 else "warning"
    return FleetAlert(
        repo=repo,
        kind="error_spike",
        severity=severity,
        summary=(
            f"error_count={latest.error_count} ≥ prior_mean({mean_prior:.1f}) × {multiplier:g}"
        ),
        opened_at=latest.run_at,
        details={
            "latest_error_count": latest.error_count,
            "prior_mean": mean_prior,
            "multiplier": multiplier,
        },
    )


def _ghosted_alert(
    repo: str,
    rows: list[FleetHeartbeat],
    *,
    window_days: int,
    now: datetime,
) -> FleetAlert | None:
    if not rows or window_days < 1:
        return None
    latest = rows[-1]
    last_seen = latest.run_at if latest.run_at.tzinfo else latest.run_at.replace(tzinfo=UTC)
    cutoff = now - timedelta(days=window_days)
    if last_seen >= cutoff:
        return None
    age_days = (now - last_seen).days
    severity: AlertSeverity = "critical" if age_days >= window_days * 2 else "warning"
    return FleetAlert(
        repo=repo,
        kind="ghosted",
        severity=severity,
        summary=f"no heartbeat in {age_days} days (window={window_days}d)",
        opened_at=last_seen,
        details={
            "last_seen": last_seen.isoformat(),
            "window_days": window_days,
            "age_days": age_days,
        },
    )


def _scope_gap_alert(
    repo: str,
    rows: list[FleetHeartbeat],
    *,
    now: datetime,
) -> FleetAlert | None:
    if not rows:
        return None
    latest = rows[-1]
    summary = latest.summary or {}
    # Accept a handful of shapes the scope-gap reporter might emit:
    #   {"scope_gap_open": true}
    #   {"scope_gap": {"open": true, "count": 3, "scope_hint": "workflow"}}
    #   {"scope_gap_count": 4}
    flag = bool(summary.get("scope_gap_open"))
    nested = summary.get("scope_gap")
    if isinstance(nested, dict):
        flag = flag or bool(nested.get("open")) or int(nested.get("count", 0) or 0) > 0
    count = int(summary.get("scope_gap_count", 0) or 0)
    if count > 0:
        flag = True
    if not flag:
        return None
    hint = ""
    if isinstance(nested, dict):
        hint = str(nested.get("scope_hint", "") or "")
    hint_part = f" ({hint})" if hint else ""
    return FleetAlert(
        repo=repo,
        kind="scope_gap",
        severity="warning",
        summary=f"scope-gap tracker reports missing token scope{hint_part}",
        opened_at=latest.run_at,
        details={k: v for k, v in summary.items() if str(k).startswith("scope_gap")},
    )


def evaluate_fleet_alerts(
    recent_heartbeats: Iterable[FleetHeartbeat | dict[str, Any]],
    *,
    goal_health_threshold: float = 0.7,
    goal_health_n_consecutive: int = 3,
    error_spike_multiplier: float = 3.0,
    ghosted_window_days: int = 7,
    now: datetime | None = None,
) -> list[FleetAlert]:
    """Evaluate alert conditions over a batch of heartbeats.

    Pure function — no graph / store writes. The caller is expected to pass
    the cross-repo heartbeat history (see
    :meth:`FleetRegistryStore.recent_heartbeats`) and then forward the
    returned list to :func:`upsert_fleet_alerts` +
    :meth:`FleetAlertStore.apply` for persistence + resolution.

    ``now`` is injectable for tests; defaults to wall-clock UTC.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    grouped = _group_by_repo(recent_heartbeats)
    alerts: list[FleetAlert] = []
    for repo, rows in grouped.items():
        gh = _goal_health_alert(
            repo,
            rows,
            threshold=goal_health_threshold,
            n_consecutive=goal_health_n_consecutive,
            now=now,
        )
        if gh is not None:
            alerts.append(gh)
        spike = _error_spike_alert(
            repo,
            rows,
            multiplier=error_spike_multiplier,
            now=now,
        )
        if spike is not None:
            alerts.append(spike)
        ghosted = _ghosted_alert(
            repo,
            rows,
            window_days=ghosted_window_days,
            now=now,
        )
        if ghosted is not None:
            alerts.append(ghosted)
        scope_gap = _scope_gap_alert(repo, rows, now=now)
        if scope_gap is not None:
            alerts.append(scope_gap)
    alerts.sort(key=lambda a: (a.repo, a.kind))
    return alerts


# ── Graph sync ──────────────────────────────────────────────────────────


class _GraphStoreProtocol(Protocol):
    """Duck-type the subset of :class:`GraphStore` the alert sync uses."""

    async def merge_node(self, label: str, node_id: str, properties: dict[str, Any]) -> None: ...


def _alert_node_id(alert: FleetAlert) -> str:
    # ``(repo, kind)`` is the dedup key; rolling ``opened_at`` into the id
    # would defeat resolution tracking because a new node would be created
    # each time the alert re-fires.
    safe_repo = alert.repo.replace("/", "_")
    return f"fleet_alert:{safe_repo}:{alert.kind}"


def upsert_fleet_alerts(
    alerts: list[FleetAlert],
    *,
    graph: _GraphStoreProtocol | None,
) -> None:
    """Mirror alerts into the graph as ``:FleetAlert`` nodes (best-effort).

    Invoked synchronously from the admin evaluation pass. The graph store is
    async-native, so we schedule a task on the running loop if one is
    available and otherwise fall back to ``asyncio.run`` for one-shot CLI
    contexts. Failures are logged at WARNING and swallowed — alerting must
    not cascade into an admin outage.
    """
    if graph is None:
        return

    async def _run() -> None:
        for alert in alerts:
            props: dict[str, Any] = {
                "repo": alert.repo,
                "kind": alert.kind,
                "severity": alert.severity,
                "summary": alert.summary,
                "opened_at": alert.opened_at.isoformat(),
                "resolved_at": (
                    alert.resolved_at.isoformat() if alert.resolved_at is not None else None
                ),
                "status": "resolved" if alert.resolved_at is not None else "open",
            }
            try:
                await graph.merge_node(NodeType.FLEET_ALERT, _alert_node_id(alert), props)
            except Exception as exc:  # best-effort: never cascade
                logger.warning(
                    "upsert_fleet_alerts: graph write failed for %s: %s",
                    alert.repo,
                    exc,
                )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Fire-and-forget on the running loop (e.g. from an async admin
        # handler). The resulting task is allowed to complete on its own;
        # we don't await it because the admin request should not block on
        # the downstream graph store.
        loop.create_task(_run())
    else:
        try:
            asyncio.run(_run())
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("upsert_fleet_alerts: asyncio.run failed: %s", exc)


# ── In-memory alert store (admin-side) ──────────────────────────────────


class FleetAlertStore:
    """In-memory alert registry keyed on ``(repo, kind)``.

    Lives alongside :class:`FleetRegistryStore` on the admin backend. Two
    operations:

    * :meth:`apply` — merge the output of :func:`evaluate_fleet_alerts`
      into the store. Existing alerts with the same ``(repo, kind)`` are
      updated in place (preserves original ``opened_at``). Alerts that are
      no longer reported by the evaluator get their ``resolved_at`` set to
      ``now`` (one-shot: a subsequent :meth:`apply` that doesn't include
      them leaves ``resolved_at`` unchanged).
    * :meth:`list` — read alerts; ``open_only=True`` filters out resolved.
    """

    def __init__(self) -> None:
        self._alerts: dict[tuple[str, AlertKind], FleetAlert] = {}
        self._lock = asyncio.Lock()

    async def apply(
        self,
        evaluated: list[FleetAlert],
        *,
        now: datetime | None = None,
    ) -> list[FleetAlert]:
        """Merge evaluator output; resolve alerts that no longer fire.

        Returns the resulting alert set (both open + freshly-resolved), in
        ``(repo, kind)`` order. Deterministic.
        """
        current_keys = {(a.repo, a.kind) for a in evaluated}
        stamp = now or datetime.now(UTC)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        async with self._lock:
            # Upsert active evaluator rows.
            for alert in evaluated:
                key = (alert.repo, alert.kind)
                existing = self._alerts.get(key)
                if existing is None or not existing.is_open():
                    # Fresh alert, or a previously-resolved one re-firing.
                    # Either way a new ``opened_at`` is appropriate.
                    self._alerts[key] = FleetAlert(
                        repo=alert.repo,
                        kind=alert.kind,
                        severity=alert.severity,
                        summary=alert.summary,
                        opened_at=alert.opened_at,
                        resolved_at=None,
                        details=dict(alert.details),
                    )
                else:
                    # Update mutable fields but keep ``opened_at`` and
                    # resolved_at=None so the alert's lifetime is preserved.
                    existing.severity = alert.severity
                    existing.summary = alert.summary
                    existing.details = dict(alert.details)
            # Resolve any stored alert the evaluator did not re-emit.
            for key, alert in self._alerts.items():
                if alert.is_open() and key not in current_keys:
                    alert.resolved_at = stamp
            return sorted(self._alerts.values(), key=lambda a: (a.repo, a.kind))

    async def list(self, *, open_only: bool = False) -> list[FleetAlert]:
        async with self._lock:
            rows = list(self._alerts.values())
        if open_only:
            rows = [r for r in rows if r.is_open()]
        rows.sort(key=lambda a: (a.repo, a.kind))
        return rows

    async def clear(self) -> None:
        """Test helper — drop all stored alerts."""
        async with self._lock:
            self._alerts.clear()


# Module singleton. Tests that need isolation call :func:`reset_alert_store_for_tests`.
_ALERT_STORE = FleetAlertStore()


def get_alert_store() -> FleetAlertStore:
    return _ALERT_STORE


def reset_alert_store_for_tests() -> None:
    global _ALERT_STORE  # noqa: PLW0603
    _ALERT_STORE = FleetAlertStore()


__all__ = [
    "AlertKind",
    "AlertSeverity",
    "FleetAlert",
    "FleetAlertStore",
    "evaluate_fleet_alerts",
    "get_alert_store",
    "reset_alert_store_for_tests",
    "upsert_fleet_alerts",
]
