"""MemoryBackend — abstract protocol for caretaker agent memory storage.

All implementations must satisfy this interface.  The orchestrator and agents
program against ``MemoryBackend`` only; they never import a concrete class
directly so the backing store can be swapped via configuration.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    """Namespaced key-value store used by caretaker agents.

    Keys within a namespace are arbitrary strings.  Values are always
    stored as strings; use ``get_json`` / ``set_json`` helpers for
    structured data.

    Implementations MUST be safe to call synchronously (all methods are
    regular, not async) so they can be used from both sync and async
    contexts without friction.
    """

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, namespace: str, key: str) -> str | None:
        """Return the stored value or ``None`` if absent / expired."""
        ...

    def get_json(self, namespace: str, key: str) -> Any:
        """Return the stored value parsed from JSON, or ``None`` if absent."""
        ...

    def list_keys(self, namespace: str) -> list[str]:
        """Return all non-expired keys in *namespace*, ordered newest-first."""
        ...

    def all_entries(self, namespace: str) -> dict[str, str]:
        """Return all non-expired key→value pairs in *namespace*."""
        ...

    # ── Write ─────────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store *value* under *namespace*/*key* with an optional TTL."""
        ...

    def set_json(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl_seconds: int | None = None,
    ) -> None:
        """Serialize *value* to JSON then store it."""
        ...

    def delete(self, namespace: str, key: str) -> None:
        """Remove a single entry.  No-op if absent."""
        ...

    # ── Maintenance ───────────────────────────────────────────────────────

    def prune_expired(self) -> int:
        """Delete all expired entries; return count removed."""
        ...

    def snapshot_json(self) -> str:
        """Return the full (non-expired) store as a JSON string (for artifacts)."""
        ...

    def close(self) -> None:
        """Release any held resources (connections, file handles)."""
        ...
