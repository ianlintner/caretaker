"""Offline evaluation harness over caretaker's shadow-decision stream.

Workstream A4 of the R&D master plan. The admin heatmap gives operators
a vibes-level signal for ``shadow → enforce`` flips; this package turns
that signal into a repeatable offline evaluation with per-site agreement
rates, an optional LLM judge for free-text fields, and an enforce-gate
threshold that CI can check before merging a mode-flip PR.

Public surface:

* :func:`run_nightly_eval` — aggregate the last N hours of
  ``:ShadowDecision`` records, score them per site, emit Prometheus
  gauges, and (when the ``braintrust`` extra is installed and
  ``BRAINTRUST_API_KEY`` is set) log one experiment per site.
* :class:`NightlyReport` / :class:`SiteReport` — the structured result,
  serialisable to JSON so the CI workflow can post a PR comment.
* :mod:`caretaker.eval.scorers` — per-site exact-match scorers plus the
  LLM judge for the readiness ``summary`` free-text field.
* :mod:`caretaker.eval.braintrust_client` — thin dependency-injectable
  wrapper around the ``braintrust`` Python SDK. Fail-open when the SDK
  or API key is missing so the harness degrades to a local-only run.

No shadow-persistence code is touched: the harness is strictly a
read-only consumer of :mod:`caretaker.evolution.shadow`.
"""

from __future__ import annotations

from caretaker.eval.braintrust_client import (
    BraintrustClient,
    BraintrustUnavailable,
    get_default_client,
)
from caretaker.eval.harness import (
    EVAL_AGREEMENT_RATE,
    NightlyReport,
    SiteReport,
    run_nightly_eval,
)
from caretaker.eval.scorers import (
    DEFAULT_SCORER_REGISTRY,
    JudgeGrade,
    LLMJudge,
    Scorer,
    ScorerResult,
)

__all__ = [
    "DEFAULT_SCORER_REGISTRY",
    "EVAL_AGREEMENT_RATE",
    "BraintrustClient",
    "BraintrustUnavailable",
    "JudgeGrade",
    "LLMJudge",
    "NightlyReport",
    "Scorer",
    "ScorerResult",
    "SiteReport",
    "get_default_client",
    "run_nightly_eval",
]
