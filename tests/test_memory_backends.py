"""Tests for the MemoryBackend protocol implementations."""

from __future__ import annotations

import pytest

from caretaker.state.backends.sqlite_backend import SQLiteMemoryBackend
from caretaker.state.memory import MemoryStore


@pytest.fixture
def sqlite_backend() -> SQLiteMemoryBackend:
    """In-memory SQLite backend for isolation."""
    return SQLiteMemoryBackend(MemoryStore(db_path=":memory:"))


class TestSQLiteBackend:
    def test_set_and_get(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "k", "val")
        assert sqlite_backend.get("ns", "k") == "val"

    def test_get_missing_returns_none(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        assert sqlite_backend.get("ns", "missing") is None

    def test_set_overwrites(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "k", "first")
        sqlite_backend.set("ns", "k", "second")
        assert sqlite_backend.get("ns", "k") == "second"

    def test_namespaces_isolated(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns1", "k", "a")
        sqlite_backend.set("ns2", "k", "b")
        assert sqlite_backend.get("ns1", "k") == "a"
        assert sqlite_backend.get("ns2", "k") == "b"

    def test_delete(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "k", "v")
        sqlite_backend.delete("ns", "k")
        assert sqlite_backend.get("ns", "k") is None

    def test_set_json_and_get_json(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        payload = {"x": 1, "y": [1, 2]}
        sqlite_backend.set_json("ns", "k", payload)
        assert sqlite_backend.get_json("ns", "k") == payload

    def test_list_keys(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "a", "1")
        sqlite_backend.set("ns", "b", "2")
        keys = sqlite_backend.list_keys("ns")
        assert "a" in keys and "b" in keys

    def test_all_entries(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "k1", "v1")
        sqlite_backend.set("ns", "k2", "v2")
        entries = sqlite_backend.all_entries("ns")
        assert entries.get("k1") == "v1"
        assert entries.get("k2") == "v2"

    def test_snapshot_json_is_str(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.set("ns", "k", "v")
        snap = sqlite_backend.snapshot_json()
        import json

        assert isinstance(json.loads(snap), dict)

    def test_prune_expired_does_not_raise(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.prune_expired()

    def test_close_does_not_raise(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        sqlite_backend.close()

    def test_protocol_check(self, sqlite_backend: SQLiteMemoryBackend) -> None:
        from caretaker.state.backends.base import MemoryBackend

        assert isinstance(sqlite_backend, MemoryBackend)
