"""Process-local storage for the most recent :class:`NightlyReport`.

The nightly harness writes into this store on every run; the admin API
(``GET /api/admin/eval/latest``) and the enforce-gate CI check both
read from it. Intentionally **not** a persistent cache — production
deployments should route this through Braintrust (which *is* durable)
and treat the store as a cheap local mirror so the UI and CI gate don't
have to reach out over the network on every request.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.eval.harness import NightlyReport, SiteReport

_lock = threading.Lock()
_latest: NightlyReport | None = None
_per_site_history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
"""Site name → list of (run_until, agreement_rate) tuples, oldest-first.

Bounded to the most recent 30 runs per site so the 7-day rolling mean
can be computed without unbounded memory growth when the harness runs
every hour during tests.
"""


_MAX_HISTORY_PER_SITE = 30


def store_report(report: NightlyReport) -> None:
    """Record the latest report + append to the per-site history."""
    with _lock:
        global _latest  # noqa: PLW0603
        _latest = report
        for site_report in report.sites:
            bucket = _per_site_history[site_report.site]
            bucket.append((report.until, site_report.agreement_rate()))
            if len(bucket) > _MAX_HISTORY_PER_SITE:
                del bucket[: len(bucket) - _MAX_HISTORY_PER_SITE]


def latest_report() -> NightlyReport | None:
    """Return the most recent report, or ``None`` if never stored."""
    with _lock:
        return _latest


def latest_site_report(site: str) -> SiteReport | None:
    """Return the most recent :class:`SiteReport` for ``site``, if any."""
    with _lock:
        if _latest is None:
            return None
        return _latest.site(site)


def rolling_agreement_rate(site: str, *, window_days: int = 7) -> float | None:
    """Mean agreement rate for ``site`` over the last ``window_days``.

    Returns ``None`` when there's no history — the enforce-gate treats
    that as "no data, refuse to unlock".
    """
    from datetime import UTC, timedelta

    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    with _lock:
        history = list(_per_site_history.get(site, ()))
    recent = [rate for (ts, rate) in history if ts >= cutoff]
    if not recent:
        return None
    return sum(recent) / len(recent)


def clear_for_tests() -> None:
    """Drop all stored state. Used by tests between cases."""
    with _lock:
        global _latest  # noqa: PLW0603
        _latest = None
        _per_site_history.clear()


__all__ = [
    "clear_for_tests",
    "latest_report",
    "latest_site_report",
    "rolling_agreement_rate",
    "store_report",
]
