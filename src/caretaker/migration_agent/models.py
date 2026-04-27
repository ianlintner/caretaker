"""Data models for the migration agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeprecatedUsage:
    """A deprecated API usage found in the codebase."""

    old_api: str = ""
    replacement: str = ""
    location: str = ""
    complexity: str = "simple"
    auto_fixable: bool = False


@dataclass
class MigrationStep:
    """A step in an ordered migration plan."""

    number: int = 0
    title: str = ""
    deprecations_addressed: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    risk: str = "low"


@dataclass
class MigrationReport:
    """Aggregated report for a migration agent run."""

    deprecations_found: int = 0
    fixes_applied: int = 0
    errors: list[str] = field(default_factory=list)
