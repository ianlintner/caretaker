"""Data models for the performance agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PerfAntiPattern:
    """A performance anti-pattern detected in a PR."""

    pr_number: int
    pattern: str = ""
    location: str = ""
    severity: str = "warning"
    impact: str = ""
    fix: str = ""


@dataclass
class PerfReport:
    """Aggregated report for a performance agent run."""

    prs_analyzed: int = 0
    regressions_flagged: int = 0
    errors: list[str] = field(default_factory=list)
