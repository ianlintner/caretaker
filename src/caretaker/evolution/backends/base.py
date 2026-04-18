"""EvolutionBackend protocol — storage interface for skills and mutations.

Both the SQLite and MongoDB implementations satisfy this protocol so the
InsightStore and StrategyMutator can work against either without caring
which is active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from caretaker.evolution.insight_store import Mutation, Skill


@runtime_checkable
class EvolutionBackend(Protocol):
    """Persistence interface for the evolution layer."""

    # ── Skill operations ──────────────────────────────────────────────────

    def upsert_skill_success(self, skill_id: str, category: str, signature: str, sop: str) -> None:
        """Create or update a skill, incrementing success_count."""
        ...

    def upsert_skill_failure(self, skill_id: str, category: str, signature: str) -> None:
        """Create or update a skill, incrementing fail_count."""
        ...

    def query_skills(
        self,
        category: str,
        min_attempts: int = 3,
        limit: int = 10,
    ) -> list[Skill]:
        """Return skills for *category* with at least *min_attempts* total, sorted by confidence desc."""
        ...

    def get_skill(self, skill_id: str) -> Skill | None:
        """Return a single skill by ID, or None."""
        ...

    def all_skills(self, category: str | None = None) -> list[Skill]:
        """Return all skills, optionally filtered by category."""
        ...

    def delete_skills(self, skill_ids: list[str]) -> int:
        """Delete skills by ID. Returns count removed."""
        ...

    # ── Mutation operations ───────────────────────────────────────────────

    def upsert_mutation(self, mutation: Mutation) -> None:
        """Insert or update a mutation record."""
        ...

    def active_mutations(self) -> list[Mutation]:
        """Return all pending/in-flight mutations."""
        ...

    def mutation_history(self, limit: int = 50) -> list[Mutation]:
        """Return recent mutations ordered by start time desc."""
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Release any held resources."""
        ...
