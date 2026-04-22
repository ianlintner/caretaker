"""Aggregate per-run GitHub token scope-gap incidents.

When caretaker runs under a workflow ``GITHUB_TOKEN`` that's missing a
required permission, GitHub answers with ``403 Forbidden`` and a body of
``{"message": "Resource not accessible by integration"}``. Each call
site used to log a lone ``logger.warning`` line and move on, which meant
the consumer saw five silent warnings scattered across the run and no
single user-visible signal that half the surface area was disabled.

This module collects those incidents in a process-wide tracker keyed by
``(endpoint_template, http_method)`` so the orchestrator can emit a
single actionable issue at the end of the run — see
:mod:`caretaker.github_client.scope_gap_reporter`.

The tracker is deliberately decoupled from the issue writer: any other
operational surface (metrics, admin UI, run summary) can drain the
snapshot without owning the issue contract.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Endpoint → required-scope map ───────────────────────────────────────
#
# Keys are regex patterns matched against the request path. The first
# match wins, so narrower patterns must precede broader ones. Values are
# the GitHub fine-grained permission the workflow token is missing
# (format: ``<scope_key>: <level>``). These are the scopes consumers
# need to declare in ``.github/workflows/*.yml`` under ``permissions:``.
#
# We don't try to be exhaustive — we cover exactly the surface that has
# been observed to 403 in real runs, plus a handful of adjacent
# endpoints that sit on the same scope. Unknown endpoints fall back to
# ``metadata: read`` with the raw endpoint surfaced in the report so
# maintainers can extend the map.

_ENDPOINT_SCOPE_MAP: tuple[tuple[str, str, str], ...] = (
    # (regex, method-filter, scope-hint)
    # method-filter is either "*" or an explicit HTTP verb.
    (r"^/repos/[^/]+/[^/]+/dependabot/alerts", "*", "security_events: read"),
    (r"^/repos/[^/]+/[^/]+/code-scanning/alerts", "*", "security_events: read"),
    (r"^/repos/[^/]+/[^/]+/secret-scanning/alerts", "*", "security_events: read"),
    (r"^/repos/[^/]+/[^/]+/pulls(?:/|$)", "POST", "pull_requests: write"),
    (r"^/repos/[^/]+/[^/]+/pulls/[0-9]+/requested_reviewers", "*", "pull_requests: write"),
    (r"^/repos/[^/]+/[^/]+/pulls/[0-9]+/reviews", "POST", "pull_requests: write"),
    (r"^/repos/[^/]+/[^/]+/pulls/[0-9]+/merge", "*", "pull_requests: write"),
    (r"^/repos/[^/]+/[^/]+/issues/[0-9]+/assignees", "*", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/issues(?:/|$)", "POST", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/issues/[0-9]+(?:/|$)", "PATCH", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/issues/[0-9]+/comments", "*", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/issues/comments/[0-9]+", "*", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/issues/[0-9]+/labels", "*", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/labels(?:/|$)", "*", "issues: write"),
    (r"^/repos/[^/]+/[^/]+/contents/", "*", "contents: write"),
    (r"^/repos/[^/]+/[^/]+/git/refs", "*", "contents: write"),
    (r"^/repos/[^/]+/[^/]+/check-runs", "*", "checks: write"),
    (r"^/repos/[^/]+/[^/]+/actions/runs/[0-9]+/rerun", "*", "actions: write"),
    (r"^/repos/[^/]+/[^/]+/actions/runs/[0-9]+/approve", "*", "actions: write"),
    (r"^/repos/[^/]+/[^/]+/milestones", "*", "issues: write"),
)

_UNKNOWN_SCOPE_HINT = "metadata: read"


# ── Regex templates for endpoint normalization ──────────────────────────
#
# We dedupe per ``(endpoint_template, method)`` rather than per raw
# path so that two 403s on ``/repos/o/r/pulls/5/merge`` and
# ``/repos/o/r/pulls/6/merge`` collapse into one incident.

_NUM_PATH_SEGMENT = re.compile(r"/[0-9]+(?=/|$)")


def _template_path(path: str) -> str:
    """Normalize a raw path to a template (numeric IDs → ``:n``).

    Owner and repo slugs are preserved because caretaker runs are
    always single-consumer-repo — all incidents in a run carry the
    same ``{owner}/{repo}`` anyway, and keeping them helps the report
    tell the reader which repo it's describing.
    """
    return _NUM_PATH_SEGMENT.sub("/:n", path)


def infer_scope_hint(method: str, path: str) -> str:
    """Return the GitHub scope required for ``method`` + ``path``.

    Falls back to :data:`_UNKNOWN_SCOPE_HINT` when no pattern matches.
    The caller can distinguish "known" from "unknown" by comparing to
    that constant.
    """
    upper_method = method.upper()
    for pattern, method_filter, hint in _ENDPOINT_SCOPE_MAP:
        if method_filter != "*" and method_filter != upper_method:
            continue
        if re.search(pattern, path):
            return hint
    return _UNKNOWN_SCOPE_HINT


# ── Tracker ─────────────────────────────────────────────────────────────


@dataclass
class ScopeGapIncident:
    """A single deduped scope-gap incident.

    ``count`` increments every time the same ``(endpoint_template,
    method)`` pair 403s, so the report can show frequency without
    carrying every raw path.
    """

    scope_hint: str
    endpoint: str
    method: str
    first_seen_ts: float
    count: int = 1
    example_paths: list[str] = field(default_factory=list)


class ScopeGapTracker:
    """Process-wide aggregator for GitHub token scope-gap incidents.

    Thread-safe: agents run concurrently in the orchestrator and may
    hit 403s from different coroutines scheduled on the same loop.
    """

    _MAX_EXAMPLE_PATHS = 5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._incidents: dict[tuple[str, str], ScopeGapIncident] = {}

    # ── Recording ──────────────────────────────────────────────────

    def record(self, method: str, path: str, *, ts: float | None = None) -> ScopeGapIncident:
        """Record a scope-gap 403. Returns the deduped incident row.

        Safe to call from any coroutine. Dedupe key is the endpoint
        *template* (numeric IDs stripped) × HTTP method. The raw path
        is kept as an example (capped) so reports can point at a
        concrete 403.
        """
        upper_method = method.upper()
        template = _template_path(path)
        key = (template, upper_method)
        now = ts if ts is not None else time.time()
        scope_hint = infer_scope_hint(upper_method, path)

        with self._lock:
            incident = self._incidents.get(key)
            if incident is None:
                incident = ScopeGapIncident(
                    scope_hint=scope_hint,
                    endpoint=template,
                    method=upper_method,
                    first_seen_ts=now,
                    count=1,
                    example_paths=[path],
                )
                self._incidents[key] = incident
            else:
                incident.count += 1
                if (
                    path not in incident.example_paths
                    and len(incident.example_paths) < self._MAX_EXAMPLE_PATHS
                ):
                    incident.example_paths.append(path)
            # Copy for the caller so they can't mutate the stored row
            # behind the lock.
            return ScopeGapIncident(
                scope_hint=incident.scope_hint,
                endpoint=incident.endpoint,
                method=incident.method,
                first_seen_ts=incident.first_seen_ts,
                count=incident.count,
                example_paths=list(incident.example_paths),
            )

    # ── Queries ────────────────────────────────────────────────────

    def snapshot(self) -> list[ScopeGapIncident]:
        """Return a list copy of all tracked incidents.

        Sorted by ``scope_hint`` then ``endpoint`` so the report output
        is deterministic across runs.
        """
        with self._lock:
            rows = [
                ScopeGapIncident(
                    scope_hint=i.scope_hint,
                    endpoint=i.endpoint,
                    method=i.method,
                    first_seen_ts=i.first_seen_ts,
                    count=i.count,
                    example_paths=list(i.example_paths),
                )
                for i in self._incidents.values()
            ]
        rows.sort(key=lambda r: (r.scope_hint, r.endpoint, r.method))
        return rows

    def is_empty(self) -> bool:
        with self._lock:
            return not self._incidents

    def grouped_by_scope(self) -> dict[str, list[ScopeGapIncident]]:
        """Return incidents grouped by ``scope_hint`` for the report.

        The grouping preserves the deterministic ordering of
        :meth:`snapshot`.
        """
        grouped: dict[str, list[ScopeGapIncident]] = {}
        for incident in self.snapshot():
            grouped.setdefault(incident.scope_hint, []).append(incident)
        return grouped

    # ── Test / reset ───────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all tracked incidents (used between runs and in tests)."""
        with self._lock:
            self._incidents.clear()


# Process-wide singleton. Every :class:`GitHubClient` in the same run
# feeds the same tracker so one unified issue is emitted.
_TRACKER = ScopeGapTracker()


def get_tracker() -> ScopeGapTracker:
    return _TRACKER


def reset_tracker() -> None:
    """Clear the per-run singleton. Called between orchestrator runs."""
    _TRACKER.reset()


def reset_for_tests() -> None:
    """Alias of :func:`reset_tracker` retained for test-file clarity."""
    _TRACKER.reset()


# ── Metrics bridge ─────────────────────────────────────────────────────


def record_scope_gap_metric(scope_hint: str) -> None:
    """Bump the Prometheus counter for ``scope_hint``.

    Deferred import: the metrics module is optional in minimal test
    environments, and observability must never cascade.
    """
    try:
        from caretaker.observability.metrics import record_github_scope_gap
    except Exception:  # pragma: no cover - observability must never cascade
        return
    with contextlib.suppress(Exception):  # pragma: no cover
        record_github_scope_gap(scope_hint)


# ── 403 body sniffing ─────────────────────────────────────────────────


_SCOPE_GAP_MARKERS: tuple[str, ...] = (
    "resource not accessible by integration",
    "resource not accessible by personal access token",
    # GitHub's org-level gate — same actionable fix (add scope / approve install).
    "must have admin rights",
)


def is_scope_gap_message(message: str) -> bool:
    """Return True when a 403 body looks like a token-scope gap.

    We match on the specific "Resource not accessible by integration"
    GitHub returns for missing workflow scopes, plus a small set of
    equivalent phrasings. Importantly we do *not* match on "Bad
    credentials" (that's an invalid-token problem, not a scope
    problem) or on the rate-limit prefix.
    """
    if not message:
        return False
    lowered = message.casefold()
    return any(marker in lowered for marker in _SCOPE_GAP_MARKERS)


__all__ = [
    "ScopeGapIncident",
    "ScopeGapTracker",
    "get_tracker",
    "infer_scope_hint",
    "is_scope_gap_message",
    "record_scope_gap_metric",
    "reset_for_tests",
    "reset_tracker",
]
