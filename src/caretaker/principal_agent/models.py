"""Data models for the principal agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ArchReviewResult:
    """Result of an architectural review on a PR."""

    pr_number: int
    verdict: str = ""
    summary: str = ""
    findings: list[str] = field(default_factory=list)


@dataclass
class PRDResult:
    """Result of PRD generation for an issue."""

    issue_number: int
    prd_text: str = ""
    comment_id: int | None = None


@dataclass
class RefactorPlan:
    """Result of refactor decomposition."""

    issue_number: int
    plan_text: str = ""
    steps: int = 0


@dataclass
class PrincipalReport:
    """Aggregated report for a principal agent run."""

    reviews_completed: int = 0
    prds_created: int = 0
    refactors_planned: int = 0
    errors: list[str] = field(default_factory=list)
