"""MongoEvolutionBackend — MongoDB/Cosmos DB backend for skills and mutations.

Enabled when ``mongo.enabled = true`` and ``evolution.backend = "mongo"``
in ``.caretaker.yml``.  Requires the ``motor`` package (installed via the
``backend`` extra: ``pip install caretaker[backend]``).

Uses two collections:
- ``evolution_skills``   — one document per problem signature
- ``evolution_mutations`` — one document per strategy mutation trial

Key advantages over SQLite:
- Survives across GitHub Actions runs without ``actions/cache`` (durable cloud storage)
- ``$inc`` for atomic counter updates — safe under concurrent orchestrator processes
- Native TTL index for future skill expiry support
- Works with the same Cosmos DB / Atlas cluster as the MemoryBackend
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pymongo
    import pymongo.collection

logger = logging.getLogger(__name__)

_SKILL_FIELDS = {
    "_id",
    "category",
    "signature",
    "sop_text",
    "success_count",
    "fail_count",
    "last_used_at",
    "created_at",
}
_MUTATION_FIELDS = {
    "_id",
    "agent_name",
    "parameter",
    "old_value",
    "new_value",
    "goal_id",
    "goal_score_before",
    "goal_score_after",
    "runs_evaluated",
    "started_at",
    "ended_at",
    "outcome",
}


def _to_skill(doc: dict[str, Any]) -> Any:
    from caretaker.evolution.insight_store import Skill, _parse_dt

    return Skill(
        id=str(doc["_id"]),
        category=doc["category"],
        signature=doc["signature"],
        sop_text=doc.get("sop_text", ""),
        success_count=doc.get("success_count", 0),
        fail_count=doc.get("fail_count", 0),
        last_used_at=_parse_dt(
            doc["last_used_at"].isoformat() if doc.get("last_used_at") else None
        ),
        created_at=_parse_dt(doc["created_at"].isoformat() if doc.get("created_at") else None)
        or datetime.now(UTC),
    )


def _to_mutation(doc: dict[str, Any]) -> Any:
    from caretaker.evolution.insight_store import Mutation, _parse_dt

    def _iso(v: Any) -> str | None:
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    return Mutation(
        id=str(doc["_id"]),
        agent_name=doc["agent_name"],
        parameter=doc["parameter"],
        old_value=doc["old_value"],
        new_value=doc["new_value"],
        goal_id=doc["goal_id"],
        goal_score_before=doc.get("goal_score_before"),
        goal_score_after=doc.get("goal_score_after"),
        runs_evaluated=doc.get("runs_evaluated", 0),
        started_at=_parse_dt(_iso(doc.get("started_at"))) or datetime.now(UTC),
        ended_at=_parse_dt(_iso(doc.get("ended_at"))),
        outcome=doc.get("outcome"),
    )


class MongoEvolutionBackend:
    """MongoDB-backed evolution storage for skills and mutations.

    Uses synchronous ``pymongo`` to match the ``InsightStore`` interface
    (no async leakage into agent code).

    Args:
        mongodb_url: Standard MongoDB connection URI.
        database_name: MongoDB database to use.
        skills_collection: Collection name for skill documents.
        mutations_collection: Collection name for mutation documents.
    """

    def __init__(
        self,
        mongodb_url: str,
        database_name: str = "caretaker",
        skills_collection: str = "evolution_skills",
        mutations_collection: str = "evolution_mutations",
    ) -> None:
        try:
            import pymongo as _pymongo
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pymongo is required for the MongoDB evolution backend. "
                "Install it with: pip install caretaker[backend]"
            ) from exc

        self._client: pymongo.MongoClient[Any] = _pymongo.MongoClient(mongodb_url)
        db = self._client[database_name]
        self._skills: pymongo.collection.Collection[Any] = db[skills_collection]
        self._mutations: pymongo.collection.Collection[Any] = db[mutations_collection]
        self._ensure_indexes()
        logger.info(
            "MongoEvolutionBackend connected (db=%s skills=%s mutations=%s)",
            database_name,
            skills_collection,
            mutations_collection,
        )

    # ── Schema bootstrap ─────────────────────────────────────────────────

    def _ensure_indexes(self) -> None:
        import pymongo

        # Skills: category for listing, (category, signature) for upsert guard
        self._skills.create_index(
            [("category", pymongo.ASCENDING)],
            name="idx_skills_category",
        )
        self._skills.create_index(
            [("category", pymongo.ASCENDING), ("signature", pymongo.ASCENDING)],
            unique=True,
            name="idx_skills_cat_sig",
        )
        # Sort index for top_skills queries
        self._skills.create_index(
            [("category", pymongo.ASCENDING), ("success_count", pymongo.DESCENDING)],
            name="idx_skills_success",
        )

        # Mutations: outcome for active_mutations filter
        self._mutations.create_index(
            [("outcome", pymongo.ASCENDING)],
            name="idx_mutations_outcome",
        )
        self._mutations.create_index(
            [("started_at", pymongo.DESCENDING)],
            name="idx_mutations_started",
        )

    # ── Skill operations ──────────────────────────────────────────────────

    def upsert_skill_success(self, skill_id: str, category: str, signature: str, sop: str) -> None:
        now = datetime.now(UTC)
        self._skills.update_one(
            {"_id": skill_id},
            {
                "$inc": {"success_count": 1},
                "$set": {
                    "sop_text": sop,
                    "last_used_at": now,
                    "category": category,
                    "signature": signature,
                },
                "$setOnInsert": {
                    "_id": skill_id,
                    "fail_count": 0,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        logger.debug("MongoEvolutionBackend: success upsert for %s/%s", category, signature)

    def upsert_skill_failure(self, skill_id: str, category: str, signature: str) -> None:
        now = datetime.now(UTC)
        self._skills.update_one(
            {"_id": skill_id},
            {
                "$inc": {"fail_count": 1},
                "$set": {"last_used_at": now, "category": category, "signature": signature},
                "$setOnInsert": {
                    "_id": skill_id,
                    "success_count": 0,
                    "sop_text": "",
                    "created_at": now,
                },
            },
            upsert=True,
        )
        logger.debug("MongoEvolutionBackend: failure upsert for %s/%s", category, signature)

    def query_skills(
        self,
        category: str,
        min_attempts: int = 3,
        limit: int = 10,
    ) -> list[Any]:
        """Return skills with total_attempts >= min_attempts, sorted by confidence desc."""
        # MongoDB doesn't support computed fields in $match, so we use $expr
        cursor = self._skills.find(
            {
                "category": category,
                "$expr": {
                    "$gte": [
                        {"$add": ["$success_count", "$fail_count"]},
                        min_attempts,
                    ]
                },
            },
            limit=limit,
            # Sort by success_count desc as a proxy for confidence (exact sort happens in Python)
            sort=[("success_count", -1)],
        )
        skills = [_to_skill(doc) for doc in cursor]
        # Sort by confidence (computed property) in Python
        skills.sort(key=lambda s: s.confidence, reverse=True)
        return skills

    def get_skill(self, skill_id: str) -> Any | None:
        doc = self._skills.find_one({"_id": skill_id})
        return _to_skill(doc) if doc else None

    def all_skills(self, category: str | None = None) -> list[Any]:
        flt: dict[str, Any] = {}
        if category:
            flt["category"] = category
        cursor = self._skills.find(flt, sort=[("success_count", -1)])
        return [_to_skill(doc) for doc in cursor]

    def delete_skills(self, skill_ids: list[str]) -> int:
        if not skill_ids:
            return 0
        result = self._skills.delete_many({"_id": {"$in": skill_ids}})
        return result.deleted_count

    # ── Mutation operations ───────────────────────────────────────────────

    def upsert_mutation(self, mutation: Any) -> None:
        doc: dict[str, Any] = {
            "_id": mutation.id,
            "agent_name": mutation.agent_name,
            "parameter": mutation.parameter,
            "old_value": mutation.old_value,
            "new_value": mutation.new_value,
            "goal_id": mutation.goal_id,
            "goal_score_before": mutation.goal_score_before,
            "goal_score_after": mutation.goal_score_after,
            "runs_evaluated": mutation.runs_evaluated,
            "started_at": mutation.started_at,
            "ended_at": mutation.ended_at,
            "outcome": mutation.outcome,
        }
        self._mutations.replace_one({"_id": mutation.id}, doc, upsert=True)

    def active_mutations(self) -> list[Any]:
        cursor = self._mutations.find({"$or": [{"outcome": None}, {"outcome": "pending"}]})
        return [_to_mutation(doc) for doc in cursor]

    def mutation_history(self, limit: int = 50) -> list[Any]:
        cursor = self._mutations.find(
            {},
            sort=[("started_at", -1)],
            limit=limit,
        )
        return [_to_mutation(doc) for doc in cursor]

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()
        logger.debug("MongoEvolutionBackend closed")


def build_mongo_evolution_backend(
    mongodb_url_env: str = "MONGODB_URL",
    database_name: str = "caretaker",
    skills_collection: str = "evolution_skills",
    mutations_collection: str = "evolution_mutations",
) -> MongoEvolutionBackend:
    """Construct a MongoEvolutionBackend from the environment.

    Raises RuntimeError when the required env var is unset.
    """
    mongodb_url = os.environ.get(mongodb_url_env, "").strip()
    if not mongodb_url:
        raise RuntimeError(
            f"Environment variable '{mongodb_url_env}' is not set. "
            "Set it to a MongoDB connection URI."
        )
    return MongoEvolutionBackend(
        mongodb_url=mongodb_url,
        database_name=database_name,
        skills_collection=skills_collection,
        mutations_collection=mutations_collection,
    )
