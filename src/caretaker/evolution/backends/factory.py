"""Factory — build the right evolution backend from configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.evolution.insight_store import GlobalSkillReader, InsightStore

logger = logging.getLogger(__name__)


def _build_global_skill_reader(config: MaintainerConfig) -> GlobalSkillReader | None:
    """Return a sync :GlobalSkill reader when fleet + graph are wired, else None.

    T-E3 closes the promotion read-loop: ``promote_global_skills`` was
    previously write-only. When ``fleet.include_global_in_prompts`` is
    true AND a graph store is enabled, we build a
    :class:`GraphBackedGlobalSkillReader` so ``InsightStore.get_relevant``
    can union local + global hits. Operators who disable either knob get
    the legacy local-only behaviour.
    """
    fleet_cfg = getattr(config, "fleet", None)
    if fleet_cfg is None or not getattr(fleet_cfg, "include_global_in_prompts", True):
        return None
    graph_cfg = getattr(config, "graph_store", None)
    if graph_cfg is None or not getattr(graph_cfg, "enabled", False):
        return None
    try:
        from caretaker.fleet.graph import GraphBackedGlobalSkillReader
        from caretaker.graph.store import GraphStore

        store = GraphStore(database=graph_cfg.database)
        return GraphBackedGlobalSkillReader(store)
    except Exception as exc:  # pragma: no cover — defensive; graph driver optional
        logger.warning("GlobalSkillReader wiring failed: %s", exc)
        return None


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

    Fleet integration (T-E3): when ``fleet.include_global_in_prompts``
    is true and a graph store is configured, the returned
    :class:`InsightStore` is wired to a
    :class:`caretaker.fleet.graph.GraphBackedGlobalSkillReader` so
    ``get_relevant`` returns the union of local and fleet-promoted hits.
    """
    from caretaker.evolution.insight_store import InsightStore

    if not config.evolution.enabled:
        return None

    reader = _build_global_skill_reader(config)
    include_global = bool(getattr(config.fleet, "include_global_in_prompts", True))

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
                return InsightStore(
                    backend=mongo_backend,
                    global_skill_reader=reader,
                    include_global=include_global,
                )
            except RuntimeError as exc:
                logger.warning(
                    "MongoDB evolution backend unavailable (%s) — falling back to SQLite", exc
                )

    # Default: SQLite InsightStore
    logger.info("Using SQLite evolution backend: %s", config.evolution.db_path)
    return InsightStore(
        db_path=config.evolution.db_path,
        global_skill_reader=reader,
        include_global=include_global,
    )
