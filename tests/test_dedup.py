"""Tests for the webhook delivery deduplication backends."""

from __future__ import annotations

import asyncio

import pytest

from caretaker.state.dedup import LocalDedup, build_dedup


class TestLocalDedup:
    @pytest.mark.asyncio
    async def test_new_delivery_is_true(self) -> None:
        dedup = LocalDedup()
        assert await dedup.is_new("abc-123") is True

    @pytest.mark.asyncio
    async def test_duplicate_delivery_is_false(self) -> None:
        dedup = LocalDedup()
        await dedup.is_new("abc-123")
        assert await dedup.is_new("abc-123") is False

    @pytest.mark.asyncio
    async def test_different_ids_are_both_new(self) -> None:
        dedup = LocalDedup()
        assert await dedup.is_new("id-1") is True
        assert await dedup.is_new("id-2") is True

    @pytest.mark.asyncio
    async def test_capacity_eviction(self) -> None:
        dedup = LocalDedup(capacity=2)
        await dedup.is_new("a")
        await dedup.is_new("b")
        # "a" should be evicted when "c" is added (capacity=2)
        assert await dedup.is_new("c") is True
        # "a" should now be considered new again
        assert await dedup.is_new("a") is True

    @pytest.mark.asyncio
    async def test_close_does_not_raise(self) -> None:
        dedup = LocalDedup()
        await dedup.close()

    @pytest.mark.asyncio
    async def test_concurrent_safety(self) -> None:
        dedup = LocalDedup()
        results = await asyncio.gather(*[dedup.is_new("shared") for _ in range(20)])
        # Exactly one True expected
        assert sum(1 for r in results if r is True) == 1


class TestBuildDedup:
    def test_returns_local_dedup_when_no_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDIS_URL", raising=False)
        dedup = build_dedup()
        assert isinstance(dedup, LocalDedup)

    def test_returns_redis_dedup_when_redis_url_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        from caretaker.state.dedup import RedisDedup

        dedup = build_dedup()
        assert isinstance(dedup, RedisDedup)
