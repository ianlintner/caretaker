"""SQLite MemoryBackend — thin wrapper around the existing MemoryStore.

This is the default back-end (memory_store.backend = "sqlite").
Full behaviour is unchanged from before Phase 1; this class simply
satisfies the new ``MemoryBackend`` protocol so the orchestrator can
treat all backends uniformly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.state.memory import MemoryStore


class SQLiteMemoryBackend:
    """Wraps ``MemoryStore`` to satisfy the ``MemoryBackend`` protocol."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        return self._store.get(namespace, key)

    def get_json(self, namespace: str, key: str) -> Any:
        return self._store.get_json(namespace, key)

    def list_keys(self, namespace: str) -> list[str]:
        return self._store.list_keys(namespace)

    def all_entries(self, namespace: str) -> dict[str, str]:
        return self._store.all_entries(namespace)

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store.set(namespace, key, value, ttl_seconds=ttl_seconds)

    def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        self._store.set_json(namespace, key, value, ttl_seconds=ttl_seconds)

    def delete(self, namespace: str, key: str) -> None:
        self._store.delete(namespace, key)

    # ── Maintenance ───────────────────────────────────────────────────────

    def prune_expired(self) -> int:
        return self._store.prune_expired()

    def snapshot_json(self) -> str:
        return self._store.snapshot_json()

    def close(self) -> None:
        self._store.close()
