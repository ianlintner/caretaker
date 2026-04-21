"""Tests for M5 cross-namespace query helpers on :class:`MemoryStore`."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from caretaker.state.memory import MemoryEntry, MemoryStore


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(db_path=":memory:")


class TestQuery:
    def test_glob_matches_prefix(self, store: MemoryStore) -> None:
        store.set("pr-agent", "last", "v1")
        store.set("pr-review", "last", "v2")
        store.set("issue-agent", "last", "v3")

        out = store.query("pr-*")
        keys = {(e.namespace, e.key) for e in out}
        assert keys == {("pr-agent", "last"), ("pr-review", "last")}
        assert all(isinstance(e, MemoryEntry) for e in out)

    def test_star_matches_every_namespace(self, store: MemoryStore) -> None:
        store.set("a", "k", "1")
        store.set("b", "k", "2")
        out = store.query("*")
        assert len(out) == 2

    def test_since_filters_out_older_rows(self, store: MemoryStore) -> None:
        store.set("agent", "old", "v1")
        time.sleep(0.01)
        cutoff = datetime.now(UTC)
        time.sleep(0.01)
        store.set("agent", "new", "v2")

        out = store.query("agent", since=cutoff)
        assert [e.key for e in out] == ["new"]

    def test_since_naive_datetime_is_treated_as_utc(self, store: MemoryStore) -> None:
        store.set("agent", "k1", "v1")
        # A past naive timestamp — should let every row through.
        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        out = store.query("agent", since=past)
        assert len(out) == 1

    def test_respects_limit(self, store: MemoryStore) -> None:
        for i in range(5):
            store.set("agent", f"k{i}", str(i))
        out = store.query("*", limit=2)
        assert len(out) == 2

    def test_limit_zero_returns_empty(self, store: MemoryStore) -> None:
        store.set("agent", "k", "v")
        assert store.query("*", limit=0) == []

    def test_expired_rows_are_excluded(self, store: MemoryStore) -> None:
        store.set("agent", "alive", "v1")
        store.set("agent", "dying", "v2", ttl_seconds=1)
        time.sleep(1.1)
        out = store.query("agent")
        keys = {e.key for e in out}
        assert keys == {"alive"}

    def test_order_is_newest_first(self, store: MemoryStore) -> None:
        store.set("agent", "first", "a")
        time.sleep(0.01)
        store.set("agent", "second", "b")
        time.sleep(0.01)
        store.set("agent", "third", "c")

        out = store.query("agent")
        assert [e.key for e in out] == ["third", "second", "first"]


class TestRecentKeys:
    def test_returns_last_n_in_order(self, store: MemoryStore) -> None:
        for i in range(5):
            store.set("agent", f"k{i}", str(i))
            time.sleep(0.005)
        out = store.recent_keys("agent", n=3)
        assert out == ["k4", "k3", "k2"]

    def test_n_zero_returns_empty(self, store: MemoryStore) -> None:
        store.set("agent", "k", "v")
        assert store.recent_keys("agent", n=0) == []

    def test_excludes_expired(self, store: MemoryStore) -> None:
        store.set("agent", "alive", "v1")
        store.set("agent", "dying", "v2", ttl_seconds=1)
        time.sleep(1.1)
        assert store.recent_keys("agent", n=5) == ["alive"]

    def test_scoped_to_namespace(self, store: MemoryStore) -> None:
        store.set("pr-agent", "a", "1")
        store.set("issue-agent", "b", "2")
        assert store.recent_keys("pr-agent") == ["a"]
        assert store.recent_keys("issue-agent") == ["b"]
