"""Tests for the disk-backed MemoryStore."""

from __future__ import annotations

import sqlite3
import time

import pytest

from caretaker.state.memory import MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    """In-memory (non-file) store for isolation."""
    return MemoryStore(db_path=":memory:")


class TestGetSet:
    def test_set_and_get(self, store: MemoryStore) -> None:
        store.set("agent", "key1", "hello")
        assert store.get("agent", "key1") == "hello"

    def test_get_missing_returns_none(self, store: MemoryStore) -> None:
        assert store.get("agent", "missing") is None

    def test_set_overwrites(self, store: MemoryStore) -> None:
        store.set("agent", "key1", "first")
        store.set("agent", "key1", "second")
        assert store.get("agent", "key1") == "second"

    def test_namespaces_are_isolated(self, store: MemoryStore) -> None:
        store.set("ns1", "key", "val-ns1")
        store.set("ns2", "key", "val-ns2")
        assert store.get("ns1", "key") == "val-ns1"
        assert store.get("ns2", "key") == "val-ns2"

    def test_delete_removes_entry(self, store: MemoryStore) -> None:
        store.set("agent", "key1", "v")
        store.delete("agent", "key1")
        assert store.get("agent", "key1") is None

    def test_delete_missing_is_noop(self, store: MemoryStore) -> None:
        store.delete("agent", "nonexistent")  # should not raise


class TestJsonHelpers:
    def test_set_json_and_get_json(self, store: MemoryStore) -> None:
        payload = {"a": 1, "b": [1, 2, 3]}
        store.set_json("agent", "data", payload)
        assert store.get_json("agent", "data") == payload

    def test_get_json_missing_returns_none(self, store: MemoryStore) -> None:
        assert store.get_json("agent", "nope") is None


class TestTTL:
    def test_entry_available_before_expiry(self, store: MemoryStore) -> None:
        store.set("agent", "ttl_key", "live", ttl_seconds=60)
        assert store.get("agent", "ttl_key") == "live"

    def test_entry_expired_returns_none(self, store: MemoryStore) -> None:
        # Use a tiny positive TTL and sleep just past it
        store.set("agent", "ttl_key", "short", ttl_seconds=1)
        time.sleep(1.1)
        assert store.get("agent", "ttl_key") is None

    def test_prune_expired_removes_and_returns_count(self, store: MemoryStore) -> None:
        store.set("agent", "k1", "v1", ttl_seconds=1)
        store.set("agent", "k2", "v2", ttl_seconds=1)
        store.set("agent", "k3", "v3")  # no TTL
        time.sleep(1.1)
        removed = store.prune_expired()
        assert removed == 2
        assert store.get("agent", "k3") == "v3"

    def test_expired_key_absent_from_list_keys(self, store: MemoryStore) -> None:
        store.set("agent", "live", "v", ttl_seconds=60)
        store.set("agent", "dead", "v", ttl_seconds=1)
        time.sleep(1.1)
        keys = store.list_keys("agent")
        assert "live" in keys
        assert "dead" not in keys


class TestListAndAllEntries:
    def test_list_keys_empty_namespace(self, store: MemoryStore) -> None:
        assert store.list_keys("empty") == []

    def test_list_keys_returns_all_non_expired(self, store: MemoryStore) -> None:
        store.set("ns", "a", "1")
        store.set("ns", "b", "2")
        keys = store.list_keys("ns")
        assert set(keys) == {"a", "b"}

    def test_all_entries(self, store: MemoryStore) -> None:
        store.set("ns", "x", "10")
        store.set("ns", "y", "20")
        entries = store.all_entries("ns")
        assert entries == {"x": "10", "y": "20"}

    def test_all_entries_empty(self, store: MemoryStore) -> None:
        assert store.all_entries("nobody") == {}


class TestNamespaceLimit:
    def test_oldest_entries_pruned_on_overflow(self) -> None:
        store = MemoryStore(db_path=":memory:", max_entries_per_namespace=3)
        for i in range(5):
            store.set("ns", f"key{i}", str(i))
        keys = store.list_keys("ns")
        assert len(keys) == 3

    def test_no_limit_when_zero(self) -> None:
        store = MemoryStore(db_path=":memory:", max_entries_per_namespace=0)
        for i in range(10):
            store.set("ns", f"key{i}", str(i))
        assert len(store.list_keys("ns")) == 10


class TestSnapshot:
    def test_snapshot_is_valid_json(self, store: MemoryStore) -> None:
        import json

        store.set("ns1", "a", "1")
        store.set("ns2", "b", "2")
        data = json.loads(store.snapshot_json())
        assert "ns1" in data
        assert "ns2" in data

    def test_snapshot_excludes_expired(self, store: MemoryStore) -> None:
        import json

        store.set("ns", "live", "v", ttl_seconds=60)
        store.set("ns", "dead", "v", ttl_seconds=1)
        time.sleep(1.1)
        data = json.loads(store.snapshot_json())
        keys = [e["key"] for e in data.get("ns", [])]
        assert "live" in keys
        assert "dead" not in keys


class TestContextManager:
    def test_context_manager_closes(self) -> None:
        with MemoryStore(db_path=":memory:") as s:
            s.set("ns", "k", "v")
            assert s.get("ns", "k") == "v"
        # Connection is closed — further operations should raise
        with pytest.raises(sqlite3.ProgrammingError):
            s.get("ns", "k")


class TestFilePersistence:
    def test_data_survives_reopen(self, tmp_path: pytest.TempPathFactory) -> None:
        db = str(tmp_path / "test.db")  # type: ignore[arg-type]
        s1 = MemoryStore(db_path=db)
        s1.set("ag", "key", "persistent")
        s1.close()

        s2 = MemoryStore(db_path=db)
        assert s2.get("ag", "key") == "persistent"
        s2.close()
