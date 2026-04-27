"""Data models for the test agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CoverageGap:
    """A test coverage gap identified in a PR."""

    pr_number: int
    description: str = ""
    priority: str = "important"


@dataclass
class FlakyTest:
    """A flaky test detected from CI history."""

    test_name: str = ""
    failure_rate: float = 0.0


@dataclass
class TestReport:
    """Aggregated report for a test agent run."""

    prs_analyzed: int = 0
    skeletons_generated: int = 0
    flaky_detected: int = 0
    errors: list[str] = field(default_factory=list)
