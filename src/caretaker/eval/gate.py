"""Enforce-gate checker for ``shadow → enforce`` promotion PRs.

A PR that flips ``agentic.<site>.mode`` from ``shadow`` to ``enforce``
must first clear the per-site agreement-rate floor defined in
``agentic.<site>.enforce_gate.min_agreement_rate``. This module diffs
the proposed config against the base config, finds any such flip, and
resolves the most recent agreement rate from :mod:`caretaker.eval.store`.

The CLI entry point (``scripts/check_enforce_gate.py``) exits with a
non-zero status if any flip fails the gate. Fails-closed when no
eval data is available: without a recent report we refuse the flip
rather than trust a first-time deployment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from caretaker.config import AgenticConfig, AgenticDomainConfig, MaintainerConfig
from caretaker.eval import store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateDecision:
    """One site's gate decision."""

    site: str
    passed: bool
    reason: str
    observed_rate: float | None
    required_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "passed": self.passed,
            "reason": self.reason,
            "observed_rate": self.observed_rate,
            "required_rate": self.required_rate,
        }


def _site_config(agentic: AgenticConfig, site: str) -> AgenticDomainConfig | None:
    """Return the ``AgenticDomainConfig`` for a given site name, or ``None``."""
    value = getattr(agentic, site, None)
    if isinstance(value, AgenticDomainConfig):
        return value
    return None


def find_flipped_sites(base: MaintainerConfig, head: MaintainerConfig) -> list[str]:
    """Return every site where ``head`` flips ``shadow → enforce``.

    Other transitions (off → shadow, enforce → shadow, etc.) are
    ignored — the gate exists specifically for the step that *removes*
    the legacy safety net, and every other transition is either
    reversible or a de-escalation.
    """
    flipped: list[str] = []
    for site in AgenticConfig.model_fields:
        base_site = _site_config(base.agentic, site)
        head_site = _site_config(head.agentic, site)
        if base_site is None or head_site is None:
            continue
        if base_site.mode == "shadow" and head_site.mode == "enforce":
            flipped.append(site)
    return flipped


def evaluate_gate(
    site: str,
    head: MaintainerConfig,
    *,
    window_days: int = 7,
) -> GateDecision:
    """Decide whether ``site`` may flip to ``enforce`` under ``head``'s gate."""
    head_site = _site_config(head.agentic, site)
    if head_site is None:
        return GateDecision(
            site=site,
            passed=False,
            reason=f"unknown site: {site}",
            observed_rate=None,
            required_rate=0.0,
        )

    required = head_site.enforce_gate.min_agreement_rate
    observed = store.rolling_agreement_rate(site, window_days=window_days)

    if observed is None:
        return GateDecision(
            site=site,
            passed=False,
            reason=(
                f"no {window_days}d eval history for site={site}; "
                "refusing flip to enforce (fail-closed)"
            ),
            observed_rate=None,
            required_rate=required,
        )
    if observed + 1e-9 < required:
        return GateDecision(
            site=site,
            passed=False,
            reason=(f"agreement_rate_{window_days}d={observed:.4f} < required={required:.4f}"),
            observed_rate=observed,
            required_rate=required,
        )
    return GateDecision(
        site=site,
        passed=True,
        reason=(f"agreement_rate_{window_days}d={observed:.4f} >= required={required:.4f}"),
        observed_rate=observed,
        required_rate=required,
    )


def check_all(
    base: MaintainerConfig,
    head: MaintainerConfig,
    *,
    window_days: int = 7,
) -> list[GateDecision]:
    """Evaluate the gate for every flipped site in ``head``."""
    decisions = []
    for site in find_flipped_sites(base, head):
        decisions.append(evaluate_gate(site, head, window_days=window_days))
    return decisions


__all__ = [
    "GateDecision",
    "check_all",
    "evaluate_gate",
    "find_flipped_sites",
]
