"""Factory — build the right MemoryBackend from configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import MaintainerConfig
    from caretaker.state.backends.base import MemoryBackend

logger = logging.getLogger(__name__)


def build_memory_backend(config: MaintainerConfig) -> MemoryBackend | None:
    """Return the active ``MemoryBackend`` or ``None`` when memory is disabled.

    Selects the backend based on ``memory_store.backend``:
    - ``"sqlite"`` — SQLite via ``MemoryStore`` (default, zero dependency).
    - ``"mongo"``  — MongoDB via ``MongoMemoryBackend`` (Phase 1).
                     Requires ``mongo.enabled = true`` and the
                     ``MONGODB_URL`` env var to be set.
    """
    if not config.memory_store.enabled:
        return None

    if config.memory_store.backend == "mongo":
        if not config.mongo.enabled:
            logger.warning(
                "memory_store.backend='mongo' but mongo.enabled=false. "
                "Falling back to SQLite."
            )
        else:
            from caretaker.state.backends.mongo_backend import build_mongo_backend

            logger.info("Using MongoDB memory backend")
            return build_mongo_backend(
                mongodb_url_env=config.mongo.mongodb_url_env,
                database_name=config.mongo.database_name,
                collection_name=config.mongo.memory_collection,
                max_entries_per_namespace=config.memory_store.max_entries_per_namespace,
            )

    # Default: SQLite
    from caretaker.state.backends.sqlite_backend import SQLiteMemoryBackend
    from caretaker.state.memory import MemoryStore

    logger.info("Using SQLite memory backend: %s", config.memory_store.db_path)
    store = MemoryStore(
        db_path=config.memory_store.db_path,
        max_entries_per_namespace=config.memory_store.max_entries_per_namespace,
    )
    return SQLiteMemoryBackend(store)

