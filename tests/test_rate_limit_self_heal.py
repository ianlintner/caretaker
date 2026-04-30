"""Tests for the cooldown self-heal background task."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from caretaker.github_client.rate_limit import (
    get_cooldown,
    reset_for_tests,
    start_cooldown_self_heal_task,
)


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    reset_for_tests()


def _make_response(*, remaining: int) -> httpx.Response:
    """Build a stub httpx.Response with rate-limit headers."""
    return httpx.Response(
        status_code=200,
        headers={
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(time.time()) + 3600),
            "X-RateLimit-Limit": "5000",
        },
        content=b'{"resources":{"core":{}}}',
    )


@pytest.mark.asyncio
async def test_self_heal_no_op_when_cooldown_clear() -> None:
    """When the cooldown is not engaged, the task does NOT call GitHub."""
    token_fetcher = AsyncMock(return_value="fake-token")
    http_get = AsyncMock(return_value=_make_response(remaining=4900))

    task = start_cooldown_self_heal_task(
        token_fetcher=token_fetcher,
        http_get=http_get,
        interval_seconds=0.05,
    )
    try:
        await asyncio.sleep(0.15)  # let the loop tick a few times
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    # Cooldown was clear → token_fetcher and http_get should NOT have been called.
    assert token_fetcher.await_count == 0
    assert http_get.await_count == 0


@pytest.mark.asyncio
async def test_self_heal_clears_stale_cooldown_when_bucket_healthy() -> None:
    """Cooldown engaged + GitHub bucket healthy → cooldown cleared after one tick."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 600.0, reason="test stuck cooldown")
    assert cd.is_blocked() is True

    token_fetcher = AsyncMock(return_value="fake-token")
    http_get = AsyncMock(return_value=_make_response(remaining=4900))

    task = start_cooldown_self_heal_task(
        token_fetcher=token_fetcher,
        http_get=http_get,
        interval_seconds=0.05,
    )
    try:
        # Wait long enough for at least one iteration to fire.
        await asyncio.sleep(0.20)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    assert token_fetcher.await_count >= 1
    assert http_get.await_count >= 1
    assert cd.is_blocked() is False, "cooldown should self-heal when bucket is healthy"


@pytest.mark.asyncio
async def test_self_heal_keeps_cooldown_when_bucket_unhealthy() -> None:
    """Cooldown engaged + bucket nearly empty → cooldown stays engaged."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 600.0, reason="test legitimate cooldown")

    token_fetcher = AsyncMock(return_value="fake-token")
    http_get = AsyncMock(return_value=_make_response(remaining=5))  # below healthy threshold

    task = start_cooldown_self_heal_task(
        token_fetcher=token_fetcher,
        http_get=http_get,
        interval_seconds=0.05,
    )
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    assert cd.is_blocked() is True, "cooldown should remain engaged when bucket is unhealthy"


@pytest.mark.asyncio
async def test_self_heal_skips_when_no_token() -> None:
    """If token_fetcher returns None, the loop logs and skips — does NOT call http_get."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 600.0, reason="test no token")

    token_fetcher = AsyncMock(return_value=None)
    http_get = AsyncMock()

    task = start_cooldown_self_heal_task(
        token_fetcher=token_fetcher,
        http_get=http_get,
        interval_seconds=0.05,
    )
    try:
        await asyncio.sleep(0.15)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    assert token_fetcher.await_count >= 1
    assert http_get.await_count == 0, "no token → no GitHub call"
    assert cd.is_blocked() is True, "cooldown stays engaged if we couldn't probe"


@pytest.mark.asyncio
async def test_self_heal_survives_http_error() -> None:
    """An exception from http_get does NOT crash the loop; subsequent iterations still run."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 600.0, reason="test transient error")

    token_fetcher = AsyncMock(return_value="fake-token")
    http_get = AsyncMock(side_effect=httpx.ConnectError("boom"))

    task = start_cooldown_self_heal_task(
        token_fetcher=token_fetcher,
        http_get=http_get,
        interval_seconds=0.05,
    )
    try:
        await asyncio.sleep(0.20)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    # Multiple failed iterations — task didn't crash.
    assert http_get.await_count >= 2
    assert cd.is_blocked() is True


# Re-export typing imports so they aren't flagged as unused by ruff.
_ = (Awaitable, Callable)
