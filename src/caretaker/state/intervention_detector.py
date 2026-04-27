"""Operator-intervention detection for the attribution telemetry subsystem.

This module answers the question: *after caretaker's most recent action on
a PR or issue, did a human push additional work?* If so, the detector
flips :attr:`~caretaker.state.models.TrackedPR.operator_intervened` and
appends a short-code reason to
:attr:`~caretaker.state.models.TrackedPR.intervention_reasons`.

The detector is intentionally event-driven rather than comment-scanning:
we look at *pushing* actions (a new commit, a manual merge, a close, a
label change, a force-push) because those are the actions a human takes
when caretaker's decision didn't stick. A human writing a comment is not
an intervention — caretaker tolerates review comments cleanly.

The detector is idempotent: re-running it over the same event stream
produces the same verdict. It never *clears* a previously-set
``operator_intervened`` flag — once flipped, the PR has been rescued by a
human and that's the truth for this week's rollup, even if a later
caretaker action lands on the same PR.

The detector is also pure: given a tracked-row snapshot and an event
stream, it returns a ``DetectionResult`` without mutating anything. The
orchestrator's state-tracker applies the result to the ``TrackedPR`` /
``TrackedIssue`` in one place so the persist path stays obvious.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from caretaker.identity import is_automated

# TrackedPR / TrackedIssue are runtime-typed (parameter defaults, isinstance-
# free attribute access). Keep them out of the TYPE_CHECKING block so Pydantic
# validation sees the concrete types at import time.
from caretaker.state.models import TrackedIssue, TrackedPR  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)


# ── Event model ─────────────────────────────────────────────────────────
#
# A single dataclass covers every event kind the detector understands.
# Keeping it narrow (six kinds, one actor login) is deliberate — the
# detector is the wrong place for taxonomy growth; if a new intervention
# kind needs support, add it here *and* to
# :data:`~caretaker.observability.metrics.INTERVENTION_REASONS` so the
# metric label set stays in sync.

INTERVENTION_KIND = frozenset({"commit", "merged", "closed", "labeled", "unlabeled", "force_push"})


@dataclass(frozen=True, slots=True)
class InterventionEvent:
    """One timestamped event from a PR or issue's timeline.

    ``kind`` must be one of :data:`INTERVENTION_KIND`. ``actor`` is the
    GitHub login of the user who performed the action; the detector uses
    :func:`caretaker.identity.is_automated` to decide whether an event is
    a human intervention or a caretaker / bot action that should be
    ignored.
    """

    kind: str
    actor: str
    occurred_at: datetime
    # Only populated for ``labeled`` / ``unlabeled`` — carries the label
    # name so the detector can skip caretaker's own ``maintainer:*``
    # labels (humans rarely add those; caretaker always does).
    label: str | None = None


# ── Detector output ──────────────────────────────────────────────────────


@dataclass
class DetectionResult:
    """Verdict produced by :func:`detect_pr_intervention`.

    ``intervened`` is True when at least one human event landed strictly
    after the tracked-row's ``last_caretaker_action_at``. ``reasons`` is
    the ordered list of short codes to append to
    :attr:`~caretaker.state.models.TrackedPR.intervention_reasons`,
    deduplicated against what's already there.
    """

    intervened: bool = False
    reasons: list[str] = field(default_factory=list)


# Map the event-kind vocabulary used in :class:`InterventionEvent` to the
# short-code vocabulary exposed as a Prometheus label and persisted in
# :attr:`TrackedPR.intervention_reasons`. Keeping two separate vocabs
# means the event source can evolve (e.g. from GitHub REST to GraphQL)
# without shifting the metric cardinality — only this map needs to know
# the bridge.
_REASON_FOR_KIND: dict[str, str] = {
    "commit": "commit_added",
    "merged": "manual_merge",
    "closed": "manual_close",
    "labeled": "label_changed",
    "unlabeled": "label_changed",
    "force_push": "force_push",
}


def _ensure_utc(stamp: datetime) -> datetime:
    """Normalise a naive datetime to UTC for comparison.

    The timeline APIs we consume return aware datetimes, but tests often
    pass naive ones through — treating naive as UTC keeps the detector
    easy to exercise without leaking tz-awareness assumptions into
    callers.
    """
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp


def _is_human_actor(actor: str) -> bool:
    """Return True when ``actor`` is a human (non-bot) GitHub login.

    Unknown / empty logins default to non-human — unattributed timeline
    events (rare, but possible for deleted accounts) are ignored rather
    than counted as interventions. Being conservative here prevents a
    bogus "operator_intervened" signal from polluting the weekly roll-up.
    """
    if not actor:
        return False
    return not is_automated(actor)


def _is_caretaker_own_label(label: str | None) -> bool:
    """Return True when ``label`` is one caretaker itself manages.

    Humans occasionally apply ``maintainer:*`` labels manually, but the
    common case is caretaker claiming ownership / marking escalation /
    etc. Treating these as non-intervention events prevents caretaker's
    own label churn from looking like human activity.
    """
    if not label:
        return False
    return label.startswith("maintainer:") or label.startswith("caretaker:")


def _collect_reasons(
    events: Iterable[InterventionEvent],
    *,
    cutoff: datetime,
) -> list[str]:
    """Return ordered intervention reason short-codes for a filtered event set.

    Deduplicated in-order so the first occurrence of each reason wins.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for event in events:
        stamp = _ensure_utc(event.occurred_at)
        if stamp <= cutoff:
            continue
        if not _is_human_actor(event.actor):
            continue
        if event.kind in {"labeled", "unlabeled"} and _is_caretaker_own_label(event.label):
            continue
        reason = _REASON_FOR_KIND.get(event.kind)
        if reason is None:
            continue
        if reason in seen:
            continue
        seen.add(reason)
        ordered.append(reason)
    return ordered


def detect_pr_intervention(
    tracking: TrackedPR,
    events: Sequence[InterventionEvent],
) -> DetectionResult:
    """Evaluate whether a human rescued a PR after caretaker's last action.

    Returns an empty ``DetectionResult`` when caretaker has never touched
    the PR (nothing to "rescue") or when the event stream is empty.
    """
    if not tracking.caretaker_touched or tracking.last_caretaker_action_at is None:
        return DetectionResult()
    cutoff = _ensure_utc(tracking.last_caretaker_action_at)
    reasons = _collect_reasons(events, cutoff=cutoff)
    if not reasons:
        return DetectionResult()
    return DetectionResult(intervened=True, reasons=reasons)


def detect_issue_intervention(
    tracking: TrackedIssue,
    events: Sequence[InterventionEvent],
) -> DetectionResult:
    """Evaluate whether a human acted on an issue after caretaker's last action.

    Issues don't have force-pushes or commits, but they do have manual
    closes and label changes. The detector accepts the same event shape
    so tests can share fixtures across PR and issue paths.
    """
    if not tracking.caretaker_touched or tracking.last_caretaker_action_at is None:
        return DetectionResult()
    cutoff = _ensure_utc(tracking.last_caretaker_action_at)
    reasons = _collect_reasons(events, cutoff=cutoff)
    if not reasons:
        return DetectionResult()
    return DetectionResult(intervened=True, reasons=reasons)


def apply_pr_detection(tracking: TrackedPR, result: DetectionResult) -> bool:
    """Merge a :class:`DetectionResult` into a :class:`TrackedPR`.

    Returns ``True`` when the tracking row's attribution state changed
    (useful for callers that want to know whether to emit a metric). The
    merge is monotonic — ``operator_intervened`` never clears, and the
    reasons list grows without duplicates.
    """
    if not result.intervened:
        return False
    changed = False
    if not tracking.operator_intervened:
        tracking.operator_intervened = True
        changed = True
    existing = set(tracking.intervention_reasons)
    for reason in result.reasons:
        if reason not in existing:
            tracking.intervention_reasons.append(reason)
            existing.add(reason)
            changed = True
    return changed


def apply_issue_detection(tracking: TrackedIssue, result: DetectionResult) -> bool:
    """Merge a :class:`DetectionResult` into a :class:`TrackedIssue`."""
    if not result.intervened:
        return False
    changed = False
    if not tracking.operator_intervened:
        tracking.operator_intervened = True
        changed = True
    existing = set(tracking.intervention_reasons)
    for reason in result.reasons:
        if reason not in existing:
            tracking.intervention_reasons.append(reason)
            existing.add(reason)
            changed = True
    return changed


# ── One-shot backfill ───────────────────────────────────────────────────


def backfill_missing_fields(
    tracked_prs: dict[int, TrackedPR],
    tracked_issues: dict[int, TrackedIssue],
) -> int:
    """Materialise attribution defaults on in-memory tracked state.

    Used under ``attribution.migration_strategy = "eager"``: walks every
    tracked row and ensures the attribution fields are present and
    coherent. Because the :class:`TrackedPR` / :class:`TrackedIssue`
    Pydantic defaults already cover the common case (missing JSON field
    → default value), this function's job is narrower — reconcile
    invariants that can only be enforced in code:

    * ``caretaker_merged = True`` implies ``caretaker_touched = True``
      (merging is inherently a touch).
    * ``caretaker_closed = True`` implies ``caretaker_touched = True``
      for issues.
    * If ``merged_at`` is set and ``caretaker_merged`` is still False,
      leave it — the prior merge may have come from a human; the
      detector / backfill CLI is the right place to decide that.

    Returns the number of rows that were mutated.
    """
    mutated = 0
    for pr in tracked_prs.values():
        if pr.caretaker_merged and not pr.caretaker_touched:
            pr.caretaker_touched = True
            mutated += 1
    for issue in tracked_issues.values():
        if issue.caretaker_closed and not issue.caretaker_touched:
            issue.caretaker_touched = True
            mutated += 1
    if mutated:
        logger.info("Attribution eager backfill: reconciled invariants on %d tracked rows", mutated)
    return mutated


__all__ = [
    "INTERVENTION_KIND",
    "DetectionResult",
    "InterventionEvent",
    "apply_issue_detection",
    "apply_pr_detection",
    "backfill_missing_fields",
    "detect_issue_intervention",
    "detect_pr_intervention",
]
