"""Factory — build the right evolution backend from configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.evolution.insight_store import InsightStore

logger = logging.getLogger(__name__)


def build_evolution_store(config: MaintainerConfig) -> InsightStore | None:
    """Return the active evolution store or None when evolution is disabled.

    Always returns an ``InsightStore`` facade so callers get the full
    high-level API (record_success, get_relevant, top_skills, etc.).
    The underlying storage backend is selected from config:

    - ``evolution.backend = "mongo"`` AND ``mongo.enabled = true``
        → InsightStore wrapping MongoEvolutionBackend
    - Anything else (or Mongo unavailable)
        → InsightStore backed by SQLite (path from ``evolution.db_path``)

    Falls back to SQLite when the MongoDB URL env var is missing.
    """
    from caretaker.evolution.insight_store import InsightStore

    if not config.evolution.enabled:
        return None

    backend_name = getattr(config.evolution, "backend", "sqlite")

    if backend_name == "mongo":
        if not config.mongo.enabled:
            logger.warning(
                "evolution.backend='mongo' but mongo.enabled=false — falling back to SQLite"
            )
        else:
            try:
                from caretaker.evolution.backends.mongo_backend import build_mongo_evolution_backend

                mongo_backend = build_mongo_evolution_backend(
                    mongodb_url_env=config.mongo.mongodb_url_env,
                    database_name=config.mongo.database_name,
                    skills_collection=config.mongo.evolution_skills_collection,
                    mutations_collection=config.mongo.evolution_mutations_collection,
                )
                logger.info("Using MongoDB evolution backend")
                return InsightStore(backend=mongo_backend)
            except RuntimeError as exc:
                logger.warning(
                    "MongoDB evolution backend unavailable (%s) — falling back to SQLite", exc
                )

    # Default: SQLite InsightStore
    logger.info("Using SQLite evolution backend: %s", config.evolution.db_path)
    return InsightStore(db_path=config.evolution.db_path)
