"""Data models for the refactor agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeSmell:
    """A code smell identified in the codebase."""

    category: str = ""
    location: str = ""
    severity: str = "minor"
    suggestion: str = ""
    confidence: float = 0.0


@dataclass
class RefactorReport:
    """Aggregated report for a refactor agent run."""

    smells_found: int = 0
    prs_created: int = 0
    errors: list[str] = field(default_factory=list)
