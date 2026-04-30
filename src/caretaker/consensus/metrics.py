"""Prometheus counters/histograms for the consensus engine."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

from caretaker.observability.metrics import REGISTRY

CONSENSUS_DECISIONS_TOTAL = Counter(
    "caretaker_consensus_decisions_total",
    "Total consensus decisions per (site, strategy, outcome).",
    ["site", "strategy", "outcome"],
    registry=REGISTRY,
)

CONSENSUS_DISAGREEMENT_TOTAL = Counter(
    "caretaker_consensus_disagreement_total",
    "AlwaysTwoModels disagreements that triggered a tiebreaker, per site.",
    ["site"],
    registry=REGISTRY,
)

CONSENSUS_UNAVAILABLE_TOTAL = Counter(
    "caretaker_consensus_unavailable_total",
    "Decisions where every model attempt errored, per site.",
    ["site"],
    registry=REGISTRY,
)

CONSENSUS_DECISION_SECONDS = Histogram(
    "caretaker_consensus_decision_seconds",
    "End-to-end engine latency including all model calls, per (site, strategy).",
    ["site", "strategy"],
    registry=REGISTRY,
)


__all__ = [
    "CONSENSUS_DECISIONS_TOTAL",
    "CONSENSUS_DECISION_SECONDS",
    "CONSENSUS_DISAGREEMENT_TOTAL",
    "CONSENSUS_UNAVAILABLE_TOTAL",
]
