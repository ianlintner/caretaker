"""InsightStore — L2/L3 skill memory for caretaker's evolution layer.

Provides a unified API for skill/mutation storage regardless of the backend
(SQLite by default, MongoDB when ``evolution.backend = "mongo"``).

When constructed directly via ``InsightStore(db_path="...")`` it uses its
own SQLite connection — identical to the previous behavior.  When constructed
via the factory (``build_evolution_store(config)``), a ``MongoEvolutionBackend``
may be used instead, with InsightStore acting as a thin facade.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.evolution.backends.base import EvolutionBackend

logger = logging.getLogger(__name__)

CATEGORY_CI = "ci"
CATEGORY_ISSUE = "issue"
CATEGORY_BUILD = "build"
CATEGORY_SECURITY = "security"
ALL_CATEGORIES = {CATEGORY_CI, CATEGORY_ISSUE, CATEGORY_BUILD, CATEGORY_SECURITY}

_DDL = """
CREATE TABLE IF NOT EXISTS skills (
    id            TEXT PRIMARY KEY,
    category      TEXT NOT NULL,
    signature     TEXT NOT NULL,
    sop_text      TEXT NOT NULL,
    success_count INTEGER DEFAULT 0,
    fail_count    INTEGER DEFAULT 0,
    last_used_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_sig ON skills(category, signature);

CREATE TABLE IF NOT EXISTS mutations (
    id                TEXT PRIMARY KEY,
    agent_name        TEXT NOT NULL,
    parameter         TEXT NOT NULL,
    old_value         TEXT NOT NULL,
    new_value         TEXT NOT NULL,
    goal_id           TEXT NOT NULL,
    goal_score_before REAL,
    goal_score_after  REAL,
    runs_evaluated    INTEGER DEFAULT 0,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    outcome           TEXT
);
CREATE INDEX IF NOT EXISTS idx_mutations_outcome ON mutations(outcome);
"""


@dataclass
class Skill:
    id: str
    category: str
    signature: str
    sop_text: str
    success_count: int
    fail_count: int
    last_used_at: datetime | None
    created_at: datetime

    @property
    def confidence(self) -> float:
        total = self.success_count + self.fail_count
        if total < 3:
            return 0.0
        return self.success_count / total

    @property
    def total_attempts(self) -> int:
        return self.success_count + self.fail_count


@dataclass
class Mutation:
    id: str
    agent_name: str
    parameter: str
    old_value: str
    new_value: str
    goal_id: str
    goal_score_before: float | None
    goal_score_after: float | None
    runs_evaluated: int
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None  # "accepted" | "rejected" | "pending" | None


def _skill_id(category: str, signature: str) -> str:
    digest = hashlib.md5(signature.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{category}:{digest}"


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _row_to_skill(row: tuple[Any, ...]) -> Skill:
    sid, category, signature, sop_text, success_count, fail_count, last_used_at, created_at = row
    return Skill(
        id=sid,
        category=category,
        signature=signature,
        sop_text=sop_text,
        success_count=success_count,
        fail_count=fail_count,
        last_used_at=_parse_dt(last_used_at),
        created_at=_parse_dt(created_at) or datetime.now(UTC),
    )


def _row_to_mutation(row: tuple[Any, ...]) -> Mutation:
    (
        mid,
        agent_name,
        parameter,
        old_value,
        new_value,
        goal_id,
        goal_score_before,
        goal_score_after,
        runs_evaluated,
        started_at,
        ended_at,
        outcome,
    ) = row
    return Mutation(
        id=mid,
        agent_name=agent_name,
        parameter=parameter,
        old_value=old_value,
        new_value=new_value,
        goal_id=goal_id,
        goal_score_before=goal_score_before,
        goal_score_after=goal_score_after,
        runs_evaluated=runs_evaluated,
        started_at=_parse_dt(started_at) or datetime.now(UTC),
        ended_at=_parse_dt(ended_at),
        outcome=outcome,
    )


class _SQLiteEvolutionBackend:
    """Self-contained SQLite backend — used when InsightStore manages its own DB."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Skill operations ──────────────────────────────────────────────────

    def upsert_skill_success(self, skill_id: str, category: str, signature: str, sop: str) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO skills (id, category, signature, sop_text, success_count, fail_count,
                                last_used_at, created_at)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                sop_text      = excluded.sop_text,
                success_count = success_count + 1,
                last_used_at  = excluded.last_used_at
            """,
            (skill_id, category, signature, sop, now, now),
        )
        self._conn.commit()

    def upsert_skill_failure(self, skill_id: str, category: str, signature: str) -> None:
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO skills (id, category, signature, sop_text, success_count, fail_count,
                                last_used_at, created_at)
            VALUES (?, ?, ?, '', 0, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fail_count   = fail_count + 1,
                last_used_at = excluded.last_used_at
            """,
            (skill_id, category, signature, now, now),
        )
        self._conn.commit()

    def query_skills(self, category: str, min_attempts: int = 3, limit: int = 10) -> list[Skill]:
        rows = self._conn.execute(
            """
            SELECT id, category, signature, sop_text, success_count, fail_count,
                   last_used_at, created_at
            FROM skills
            WHERE category = ?
              AND (success_count + fail_count) >= ?
            ORDER BY
                CAST(success_count AS REAL) / (success_count + fail_count) DESC,
                success_count DESC
            LIMIT ?
            """,
            (category, min_attempts, limit),
        ).fetchall()
        return [_row_to_skill(r) for r in rows]

    def get_skill(self, skill_id: str) -> Skill | None:
        row = self._conn.execute(
            "SELECT id, category, signature, sop_text, success_count, fail_count, "
            "last_used_at, created_at FROM skills WHERE id = ?",
            (skill_id,),
        ).fetchone()
        return _row_to_skill(row) if row else None

    def all_skills(self, category: str | None = None) -> list[Skill]:
        if category:
            rows = self._conn.execute(
                "SELECT id, category, signature, sop_text, success_count, fail_count, "
                "last_used_at, created_at FROM skills WHERE category = ? "
                "ORDER BY success_count DESC",
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, category, signature, sop_text, success_count, fail_count, "
                "last_used_at, created_at FROM skills ORDER BY category, success_count DESC"
            ).fetchall()
        return [_row_to_skill(r) for r in rows]

    def delete_skills(self, skill_ids: list[str]) -> int:
        if not skill_ids:
            return 0
        placeholders = ",".join("?" * len(skill_ids))
        cursor = self._conn.execute(f"DELETE FROM skills WHERE id IN ({placeholders})", skill_ids)
        self._conn.commit()
        return cursor.rowcount

    def upsert_mutation(self, mutation: Mutation) -> None:
        self._conn.execute(
            """
            INSERT INTO mutations (id, agent_name, parameter, old_value, new_value, goal_id,
                goal_score_before, goal_score_after, runs_evaluated, started_at, ended_at, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                goal_score_after = excluded.goal_score_after,
                runs_evaluated   = excluded.runs_evaluated,
                ended_at         = excluded.ended_at,
                outcome          = excluded.outcome
            """,
            (
                mutation.id,
                mutation.agent_name,
                mutation.parameter,
                mutation.old_value,
                mutation.new_value,
                mutation.goal_id,
                mutation.goal_score_before,
                mutation.goal_score_after,
                mutation.runs_evaluated,
                mutation.started_at.isoformat(),
                mutation.ended_at.isoformat() if mutation.ended_at else None,
                mutation.outcome,
            ),
        )
        self._conn.commit()

    def active_mutations(self) -> list[Mutation]:
        rows = self._conn.execute(
            "SELECT id, agent_name, parameter, old_value, new_value, goal_id, "
            "goal_score_before, goal_score_after, runs_evaluated, started_at, ended_at, outcome "
            "FROM mutations WHERE outcome IS NULL OR outcome = 'pending'"
        ).fetchall()
        return [_row_to_mutation(r) for r in rows]

    def mutation_history(self, limit: int = 50) -> list[Mutation]:
        rows = self._conn.execute(
            "SELECT id, agent_name, parameter, old_value, new_value, goal_id, "
            "goal_score_before, goal_score_after, runs_evaluated, started_at, ended_at, outcome "
            "FROM mutations ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_mutation(r) for r in rows]

    def close(self) -> None:
        pass  # Connection lifetime managed by InsightStore


class InsightStore:
    """Unified skill/mutation store for the evolution layer.

    Defaults to self-managed SQLite.  Can also wrap a ``MongoEvolutionBackend``
    when constructed via the factory and ``evolution.backend = "mongo"``.

    Args:
        db_path: SQLite file path (ignored when *backend* is provided).
        backend: Optional pre-built backend.  When provided, *db_path* is
                 unused and no SQLite connection is opened.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        backend: EvolutionBackend | None = None,
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

        if backend is not None:
            self._backend: EvolutionBackend = backend
        else:
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_DDL)
            self._conn.commit()
            self._backend = _SQLiteEvolutionBackend(self._conn)
            logger.debug("InsightStore opened (SQLite): %s", db_path)

    # ── Skill API ─────────────────────────────────────────────────────────

    def record_success(self, category: str, signature: str, sop: str) -> None:
        """Upsert a skill and increment its success counter."""
        self._backend.upsert_skill_success(_skill_id(category, signature), category, signature, sop)
        logger.debug("InsightStore: recorded success for '%s/%s'", category, signature)

    def record_failure(self, category: str, signature: str) -> None:
        """Upsert a skill and increment its failure counter."""
        self._backend.upsert_skill_failure(_skill_id(category, signature), category, signature)
        logger.debug("InsightStore: recorded failure for '%s/%s'", category, signature)

    def get_relevant(
        self,
        category: str,
        signature: str,
        min_confidence: float = 0.5,
    ) -> list[Skill]:
        """Return skills for this category with confidence >= min_confidence."""
        skills = self._backend.query_skills(category, min_attempts=3, limit=10)
        return [s for s in skills if s.confidence >= min_confidence]

    def get_by_signature(self, category: str, signature: str) -> Skill | None:
        """Return the skill for an exact signature, or None."""
        return self._backend.get_skill(_skill_id(category, signature))

    def top_skills(self, category: str, limit: int = 5) -> list[Skill]:
        """Return top *limit* skills by confidence for a category."""
        return self._backend.query_skills(category, min_attempts=3, limit=limit)

    def all_skills(self, category: str | None = None) -> list[Skill]:
        """Return all skills, optionally filtered by category."""
        return self._backend.all_skills(category)

    def prune_low_confidence(self, min_attempts: int = 5) -> int:
        """Delete skills with >= min_attempts but zero successes."""
        all_s = self._backend.all_skills()
        ids = [s.id for s in all_s if s.total_attempts >= min_attempts and s.success_count == 0]
        if not ids:
            return 0
        removed = self._backend.delete_skills(ids)
        if removed:
            logger.info("InsightStore: pruned %d zero-confidence skills", removed)
        return removed

    # ── Mutation API ──────────────────────────────────────────────────────

    def upsert_mutation(self, mutation: Mutation) -> None:
        self._backend.upsert_mutation(mutation)

    def active_mutations(self) -> list[Mutation]:
        return self._backend.active_mutations()

    def mutation_history(self, limit: int = 50) -> list[Mutation]:
        return self._backend.mutation_history(limit)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        self._backend.close()
        if self._conn is not None:
            self._conn.close()
            logger.debug("InsightStore closed (SQLite): %s", self._db_path)

    def __enter__(self) -> InsightStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
