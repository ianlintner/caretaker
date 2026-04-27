"""Tests for GitHub rate-limit awareness (parsing + cooldown + client)."""

from __future__ import annotations

import time

import httpx
import pytest

from caretaker.github_client.api import GitHubClient, RateLimitError
from caretaker.github_client.rate_limit import (
    get_cooldown,
    parse_rate_limit_headers,
    record_response_headers,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    reset_for_tests()


def _resp(headers: dict[str, str], status: int = 429, body: bytes = b"{}") -> httpx.Response:
    return httpx.Response(status_code=status, headers=headers, content=body)


# ── Header parsing ────────────────────────────────────────────────────


def test_parse_retry_after_seconds() -> None:
    resp = _resp({"Retry-After": "45"})
    until, remaining = parse_rate_limit_headers(resp, now=1000.0)
    assert until is not None
    assert abs(until - 1045.0) < 0.001
    assert remaining is None


def test_parse_retry_after_http_date() -> None:
    # Sun, 06 Nov 1994 08:49:37 GMT → epoch 784111777
    resp = _resp({"Retry-After": "Sun, 06 Nov 1994 08:49:37 GMT"})
    until, _ = parse_rate_limit_headers(resp, now=1000.0)
    assert until is not None
    assert abs(until - 784111777.0) < 1.0


def test_parse_x_ratelimit_reset() -> None:
    resp = _resp({"X-RateLimit-Reset": "2000"})
    until, remaining = parse_rate_limit_headers(resp, now=1000.0)
    assert until == 2000.0
    assert remaining is None


def test_parse_both_takes_later_value() -> None:
    # Retry-After 10s → 1010; Reset 2000 → later wins.
    resp = _resp({"Retry-After": "10", "X-RateLimit-Reset": "2000"})
    until, _ = parse_rate_limit_headers(resp, now=1000.0)
    assert until == 2000.0


def test_parse_remaining() -> None:
    resp = _resp({"X-RateLimit-Remaining": "3"})
    _, remaining = parse_rate_limit_headers(resp)
    assert remaining == 3


def test_parse_invalid_remaining_returns_none() -> None:
    resp = _resp({"X-RateLimit-Remaining": "not-a-number"})
    _, remaining = parse_rate_limit_headers(resp)
    assert remaining is None


# ── Cooldown state ────────────────────────────────────────────────────


def test_cooldown_starts_unblocked() -> None:
    cd = get_cooldown()
    assert cd.is_blocked() is False
    assert cd.seconds_remaining() == 0.0


def test_mark_blocked_sets_window() -> None:
    cd = get_cooldown()
    future = time.time() + 30
    cd.mark_blocked(future, reason="test")
    assert cd.is_blocked() is True
    assert cd.seconds_remaining() > 25
    assert cd.snapshot()["reason"] == "test"


def test_mark_blocked_keeps_longer_window() -> None:
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 10, reason="short")
    cd.mark_blocked(time.time() + 5, reason="even shorter")
    # The longer window (10s) must be preserved.
    assert cd.seconds_remaining() > 8


def test_mark_blocked_caps_at_max() -> None:
    # MAX is one hour by default. 10x that should be clamped.
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 36_000, reason="too long")
    assert cd.seconds_remaining() <= 3600 + 1


def test_record_response_headers_soft_throttles_on_low_remaining() -> None:
    resp = _resp({"X-RateLimit-Remaining": "5"}, status=200)
    record_response_headers(resp)
    cd = get_cooldown()
    assert cd.is_blocked()
    assert cd.snapshot()["last_remaining"] == 5


def test_record_response_headers_no_throttle_on_healthy_remaining() -> None:
    resp = _resp({"X-RateLimit-Remaining": "4000"}, status=200)
    record_response_headers(resp)
    assert get_cooldown().is_blocked() is False


# ── Client integration ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_raises_rate_limit_error_on_429(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Stub credentials so the client doesn't touch env vars.
    from caretaker.github_client.credentials import EnvCredentialsProvider

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    client = GitHubClient(credentials_provider=EnvCredentialsProvider(default_token="t"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            headers={"Retry-After": "120", "X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    with pytest.raises(RateLimitError) as exc_info:
        await client._get("/test")
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.retry_after_seconds > 60
    # Second call short-circuits without hitting the transport.
    with pytest.raises(RateLimitError) as exc_info2:
        await client._get("/other")
    assert "Short-circuit" in exc_info2.value.message


@pytest.mark.asyncio
async def test_client_raises_rate_limit_error_on_403_secondary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from caretaker.github_client.credentials import EnvCredentialsProvider

    client = GitHubClient(credentials_provider=EnvCredentialsProvider(default_token="t"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            headers={"X-RateLimit-Reset": str(int(time.time()) + 45)},
            json={"message": "You have exceeded a secondary rate limit"},
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    with pytest.raises(RateLimitError) as exc_info:
        await client._get("/test")
    assert exc_info.value.status_code == 403
    assert exc_info.value.retry_after_seconds is not None


@pytest.mark.asyncio
async def test_client_passes_through_non_rate_limited_403(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from caretaker.github_client.api import GitHubAPIError
    from caretaker.github_client.credentials import EnvCredentialsProvider

    client = GitHubClient(credentials_provider=EnvCredentialsProvider(default_token="t"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            json={"message": "Forbidden"},
        )

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    with pytest.raises(GitHubAPIError) as exc_info:
        await client._get("/test")
    assert exc_info.value.status_code == 403
    # Plain 403 is not a RateLimitError.
    assert not isinstance(exc_info.value, RateLimitError)
