"""Factory — build the right MemoryBackend from configuration."""

from __future__ import annotations

import logging

from caretaker.config import MaintainerConfig
from caretaker.state.backends.base import MemoryBackend
from caretaker.state.backends.sqlite_backend import SQLiteMemoryBackend
from caretaker.state.memory import MemoryStore

logger = logging.getLogger(__name__)


def build_memory_backend(config: MaintainerConfig) -> MemoryBackend | None:
    """Return the active ``MemoryBackend`` or ``None`` when memory is disabled.

    Selects the backend based on ``memory_store.backend``:
    - ``"sqlite"``   — SQLite via ``MemoryStore`` (default, zero dependency).
    - ``"postgres"`` — Postgres via ``PostgresMemoryBackend`` (Phase 1).
                       Requires ``postgres.enabled = true`` and the
                       ``DATABASE_URL`` env var to be set.
    """
    if not config.memory_store.enabled:
        return None

    if config.memory_store.backend == "postgres":
        if not config.postgres.enabled:
            logger.warning(
                "memory_store.backend='postgres' but postgres.enabled=false. "
                "Falling back to SQLite."
            )
        else:
            from caretaker.state.backends.postgres_backend import build_postgres_backend

            logger.info("Using Postgres memory backend")
            backend = build_postgres_backend(
                database_url_env=config.postgres.database_url_env,
                max_entries_per_namespace=config.memory_store.max_entries_per_namespace,
            )
            return backend

    # Default: SQLite
    logger.info("Using SQLite memory backend: %s", config.memory_store.db_path)
    store = MemoryStore(
        db_path=config.memory_store.db_path,
        max_entries_per_namespace=config.memory_store.max_entries_per_namespace,
    )
    return SQLiteMemoryBackend(store)
