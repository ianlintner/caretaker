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


class TestMongoBackendCosmosCompat:
    """Cosmos-DB-specific shape checks for MongoMemoryBackend.

    These don't need a live Mongo — we wire a MagicMock collection
    and assert on the call shape, because the bug class we're
    guarding against is "the Mongo call uses ``sort=`` on a field
    Cosmos hasn't indexed", not "Mongo returned the wrong rows".
    """

    def test_enforce_namespace_limit_does_not_pass_sort_to_find(self) -> None:
        """``_enforce_namespace_limit`` must not push a sort on
        ``updated_at`` ASC into Mongo: the only compound covering
        ``updated_at`` (``idx_ns_updated``) is DESC, and Cosmos's strict
        OrderBy contract refuses to satisfy an ASC sort with a DESC
        index. Sort happens in Python instead.
        """
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        from caretaker.state.backends.mongo_backend import MongoMemoryBackend

        b = MongoMemoryBackend.__new__(MongoMemoryBackend)
        b._max_entries = 2
        col = MagicMock()
        col.count_documents.return_value = 5

        # 5 docs with ascending updated_at — oldest two should be deleted.
        rows = [
            {"_id": f"id-{i}", "updated_at": datetime(2024, 1, i + 1, tzinfo=UTC)} for i in range(5)
        ]
        col.find.return_value = rows
        b._col = col

        b._enforce_namespace_limit("ns")

        # Find must be called WITHOUT ``sort=`` (Cosmos-safe).
        find_kwargs = col.find.call_args.kwargs
        assert "sort" not in find_kwargs

        # 5 docs, cap=2, so excess=3 → the three oldest (lowest
        # updated_at) ids must be deleted.
        delete_args = col.delete_many.call_args.args[0]
        assert delete_args == {"_id": {"$in": ["id-0", "id-1", "id-2"]}}

    def test_enforce_namespace_limit_under_cap_is_noop(self) -> None:
        from unittest.mock import MagicMock

        from caretaker.state.backends.mongo_backend import MongoMemoryBackend

        b = MongoMemoryBackend.__new__(MongoMemoryBackend)
        b._max_entries = 100
        col = MagicMock()
        col.count_documents.return_value = 5
        b._col = col

        b._enforce_namespace_limit("ns")
        col.find.assert_not_called()
        col.delete_many.assert_not_called()
