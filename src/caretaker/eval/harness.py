"""Nightly shadow-decision evaluation harness.

Pulls ``:ShadowDecision`` records in a time window, runs the per-site
scorer registry, and (optionally) uploads one Braintrust experiment per
site. The result is a :class:`NightlyReport` that the CLI renders as
JSON and the GitHub Actions workflow posts as a PR comment.

No network access is required on the read path — the harness goes
through :func:`caretaker.evolution.shadow.recent_records` so the
local ring-buffer fallback used in dev is honoured. Production
deployments swap in a Neo4j-backed loader via ``record_loader`` injection.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Gauge

from caretaker.eval.braintrust_client import BraintrustClient, EvalCase, ExperimentResult
from caretaker.eval.scorers import (
    DEFAULT_SCORER_REGISTRY,
    LLMJudge,
    Scorer,
    ScorerResult,
)
from caretaker.evolution.shadow import ShadowDecisionRecord, recent_records
from caretaker.observability.metrics import REGISTRY

logger = logging.getLogger(__name__)


# ── Prometheus gauge ─────────────────────────────────────────────────────
#
# Cardinality: ``site`` ≤ 10 (the AgenticConfig fields) and ``scorer``
# ≤ 12 (one per site plus the LLM judge). Upper bound: ~120 series.

EVAL_AGREEMENT_RATE = Gauge(
    "caretaker_eval_agreement_rate",
    "Rolling shadow-decision agreement rate by (site, scorer) from the nightly harness.",
    ["site", "scorer"],
    registry=REGISTRY,
)


# ── Report types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScorerSummary:
    """Aggregated scores for one (site, scorer) pair over the window."""

    scorer: str
    mean: float
    count: int
    judge_disagreements: int = 0
    """How many records the LLM-judge scorer marked failing.

    Only populated for the LLM judge; exact-match scorers leave this at
    zero so the per-row count and the disagreement count can coexist on
    one dashboard.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer": self.scorer,
            "mean": self.mean,
            "count": self.count,
            "judge_disagreements": self.judge_disagreements,
        }


@dataclass(frozen=True)
class SiteReport:
    """Per-decision-site results for one nightly run."""

    site: str
    record_count: int
    scorer_summaries: list[ScorerSummary]
    experiment_url: str | None
    braintrust_logged: bool

    def agreement_rate(self) -> float:
        """Mean across all scorer means; 1.0 when no records scored."""
        if not self.scorer_summaries:
            return 1.0
        return statistics.fmean(s.mean for s in self.scorer_summaries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "record_count": self.record_count,
            "agreement_rate": self.agreement_rate(),
            "scorer_summaries": [s.to_dict() for s in self.scorer_summaries],
            "experiment_url": self.experiment_url,
            "braintrust_logged": self.braintrust_logged,
        }


@dataclass(frozen=True)
class NightlyReport:
    """The full nightly evaluation report, one row per site."""

    since: datetime
    until: datetime
    sites: list[SiteReport]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat(),
            "until": self.until.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "sites": [s.to_dict() for s in self.sites],
        }

    def site(self, name: str) -> SiteReport | None:
        """Lookup by site name (used by tests + the admin endpoint)."""
        for s in self.sites:
            if s.site == name:
                return s
        return None


# ── Record loader (dependency injection) ─────────────────────────────────

RecordLoader = Callable[[str, datetime, datetime], Iterable[ShadowDecisionRecord]]
"""Signature: ``(site_name, since, until) → iterable of records``.

The default implementation reads the process-local ring buffer via
:func:`caretaker.evolution.shadow.recent_records`. Production callers
swap in a Neo4j-backed loader at harness wiring time.
"""


def _default_record_loader(
    site: str, since: datetime, until: datetime
) -> list[ShadowDecisionRecord]:
    records = recent_records(name=site, since=since, limit=10_000)
    return [r for r in records if r.run_at <= until]


# ── Harness entrypoint ───────────────────────────────────────────────────


def run_nightly_eval(
    since: datetime,
    until: datetime | None = None,
    *,
    sites: Sequence[str] | None = None,
    braintrust_client: BraintrustClient | None = None,
    record_loader: RecordLoader | None = None,
    llm_judge: LLMJudge | None = None,
    scorer_registry: dict[str, tuple[Scorer, ...]] | None = None,
    dry_run: bool = False,
) -> NightlyReport:
    """Evaluate shadow decisions in ``[since, until]`` per site.

    Parameters
    ----------
    since, until:
        Half-open time window; ``until`` defaults to ``datetime.now(UTC)``.
    sites:
        Subset of decision-site names. ``None`` runs every site in the
        registry.
    braintrust_client:
        Injected for tests; production uses
        :func:`caretaker.eval.braintrust_client.get_default_client`.
    record_loader:
        Injected for tests; production reads via the ring buffer.
    llm_judge:
        When provided, the readiness site also runs this judge over the
        ``summary`` free-text field.
    scorer_registry:
        Override for tests that want a minimal site list.
    dry_run:
        When ``True``, never call Braintrust (even if the client is
        available). Used by ``caretaker eval run --dry-run``.
    """
    if until is None:
        until = datetime.now(UTC)
    if since >= until:
        raise ValueError(f"since ({since}) must precede until ({until})")

    registry = scorer_registry if scorer_registry is not None else DEFAULT_SCORER_REGISTRY
    if sites is None:
        target_sites = list(registry.keys())
    else:
        unknown = [s for s in sites if s not in registry]
        if unknown:
            raise ValueError(f"unknown eval sites: {unknown}")
        target_sites = list(sites)

    loader = record_loader if record_loader is not None else _default_record_loader

    site_reports: list[SiteReport] = []
    for site in target_sites:
        scorers: list[Scorer] = list(registry[site])
        if site == "readiness" and llm_judge is not None:
            scorers.append(llm_judge)

        records = list(loader(site, since, until))
        summaries, cases = _score_records(records, scorers, site=site)

        experiment_result = _maybe_log_experiment(
            site=site,
            cases=cases,
            dry_run=dry_run,
            braintrust_client=braintrust_client,
            until=until,
        )

        for summary in summaries:
            EVAL_AGREEMENT_RATE.labels(site=site, scorer=summary.scorer).set(summary.mean)

        site_reports.append(
            SiteReport(
                site=site,
                record_count=len(records),
                scorer_summaries=summaries,
                experiment_url=experiment_result.experiment_url,
                braintrust_logged=experiment_result.logged,
            )
        )

    report = NightlyReport(since=since, until=until, sites=site_reports)
    # Mirror to the local store so the admin endpoint + enforce-gate
    # check can read the freshest numbers without replaying the whole
    # window. Deferred import: keeps the store module lazy-loadable
    # and avoids a circular import at module load.
    from caretaker.eval import store

    store.store_report(report)
    return report


# ── Internal helpers ─────────────────────────────────────────────────────


def _score_records(
    records: Sequence[ShadowDecisionRecord],
    scorers: Sequence[Scorer],
    *,
    site: str,
) -> tuple[list[ScorerSummary], list[EvalCase]]:
    """Apply each scorer to each record, collecting summaries + cases."""
    summaries: list[ScorerSummary] = []
    # Index by scorer: keep per-row results so we can build both the
    # aggregated summary (ScorerSummary) and the row-level Braintrust
    # cases (EvalCase) in a single pass.
    per_scorer_results: dict[str, list[ScorerResult]] = {}

    for scorer in scorers:
        name = getattr(scorer, "__name__", repr(scorer))
        per_scorer_results[name] = []

    cases: list[EvalCase] = []

    for record in records:
        row_scores: dict[str, float] = {}
        row_reasons: dict[str, str] = {}
        for scorer in scorers:
            name = getattr(scorer, "__name__", repr(scorer))
            result = scorer(record.legacy_verdict_json, record.candidate_verdict_json)
            per_scorer_results[name].append(result)
            row_scores[name] = result.score
            if result.reason:
                row_reasons[name] = result.reason

        cases.append(
            EvalCase(
                input={"context_json": record.context_json, "site": site},
                expected={"verdict_json": record.legacy_verdict_json},
                actual={"verdict_json": record.candidate_verdict_json or ""},
                scores=row_scores,
                metadata={
                    "record_id": record.id,
                    "run_at": record.run_at.isoformat(),
                    "repo_slug": record.repo_slug,
                    "outcome": record.outcome,
                    "mode": record.mode,
                    **({"reasons": row_reasons} if row_reasons else {}),
                },
            )
        )

    for scorer in scorers:
        name = getattr(scorer, "__name__", repr(scorer))
        results = per_scorer_results[name]
        if not results:
            summaries.append(ScorerSummary(scorer=name, mean=1.0, count=0))
            continue
        mean = statistics.fmean(r.score for r in results)
        judge_misses = sum(1 for r in results if r.metadata.get("judge_model") and r.score < 1.0)
        summaries.append(
            ScorerSummary(
                scorer=name,
                mean=mean,
                count=len(results),
                judge_disagreements=judge_misses,
            )
        )

    return summaries, cases


def _maybe_log_experiment(
    *,
    site: str,
    cases: list[EvalCase],
    dry_run: bool,
    braintrust_client: BraintrustClient | None,
    until: datetime,
) -> ExperimentResult:
    """Upload the per-site experiment unless dry-run/client missing.

    Extracted for readability: the ``if dry_run`` branch is the one
    operators hit the most (local verification of the harness before
    they flip a PR's mode knob).
    """
    if dry_run or braintrust_client is None:
        return ExperimentResult(
            name=site,
            experiment_url=None,
            case_count=len(cases),
            logged=False,
        )

    experiment_name = f"{site}-{until.strftime('%Y%m%d')}"
    return braintrust_client.log_experiment(
        experiment_name,
        cases,
        run_at=until,
        metadata={"site": site, "harness": "caretaker.eval.harness"},
    )


__all__ = [
    "EVAL_AGREEMENT_RATE",
    "NightlyReport",
    "RecordLoader",
    "ScorerSummary",
    "SiteReport",
    "run_nightly_eval",
]
