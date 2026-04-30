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


# ── Cooldown self-heal ─────────────────────────────────────────────────


def test_self_heal_clears_stale_cooldown_when_remaining_is_healthy() -> None:
    """A 403/429 sets a long cooldown; a later 200 with healthy remaining
    must clear it. Reproduces the production incident where a stale
    cooldown blocked agent dispatch despite ``X-RateLimit-Remaining``
    showing the bucket had refilled (PR #657 deferred ~1h)."""
    cd = get_cooldown()
    # Engage a long cooldown (the kind a sticky 403 would create).
    cd.mark_blocked(time.time() + 3500, reason="HTTP 403 rate-limited")
    assert cd.is_blocked() is True

    # A fresh successful response shows the bucket is healthy.
    resp = _resp({"X-RateLimit-Remaining": "11491"}, status=200)
    record_response_headers(resp)

    assert cd.is_blocked() is False
    assert cd.snapshot()["last_remaining"] == 11491
    assert cd.snapshot()["reason"] == ""


def test_self_heal_does_not_clear_when_remaining_is_low() -> None:
    """A genuinely-throttled cooldown must stay engaged when remaining
    is still below the healthy threshold — clearing here would let the
    agent burn the rest of the budget."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 60, reason="HTTP 429 rate-limited")

    # Remaining=20 is above the soft-throttle floor (15) but below the
    # healthy floor (100) — cooldown must persist.
    resp = _resp({"X-RateLimit-Remaining": "20"}, status=200)
    record_response_headers(resp)

    assert cd.is_blocked() is True
    assert cd.snapshot()["last_remaining"] == 20


def test_self_heal_clears_then_soft_throttle_does_not_re_engage_at_healthy() -> None:
    """The self-heal clears, the soft-throttle does NOT re-engage at the
    healthy reading. Net result: cooldown is fully off."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 600, reason="HTTP 429 rate-limited")

    resp = _resp({"X-RateLimit-Remaining": "5000"}, status=200)
    record_response_headers(resp)

    assert cd.is_blocked() is False
    # Soft-throttle floor is 15, so 5000 is comfortably above it.


def test_self_heal_no_op_when_already_unblocked() -> None:
    """maybe_clear_if_healthy on an unblocked cooldown is a no-op."""
    cd = get_cooldown()
    assert cd.is_blocked() is False

    cleared = cd.maybe_clear_if_healthy(5000)

    assert cleared is False
    assert cd.is_blocked() is False


def test_self_heal_no_op_when_naturally_expired() -> None:
    """An already-expired cooldown is not considered self-healed."""
    cd = get_cooldown()
    # Set a block in the past — already expired by the time we observe.
    cd.mark_blocked(time.time() - 1, reason="stale")
    cleared = cd.maybe_clear_if_healthy(5000)
    assert cleared is False


def test_self_heal_returns_true_only_on_actual_clear() -> None:
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 100, reason="active")

    # Below threshold → False, no clear.
    assert cd.maybe_clear_if_healthy(50) is False
    assert cd.is_blocked() is True

    # Above threshold → True, cleared.
    assert cd.maybe_clear_if_healthy(500) is True
    assert cd.is_blocked() is False

    # Subsequent call on already-cleared cooldown → False.
    assert cd.maybe_clear_if_healthy(500) is False


def test_self_heal_threshold_is_configurable_per_call() -> None:
    """The healthy_threshold kwarg lets ops override the env-derived default."""
    cd = get_cooldown()
    cd.mark_blocked(time.time() + 100, reason="active")

    # Caller demands a much higher bar — 50 is below it, no clear.
    assert cd.maybe_clear_if_healthy(50, healthy_threshold=1000) is False
    assert cd.is_blocked() is True

    # Below the default threshold (100) but above an explicit lower bar.
    assert cd.maybe_clear_if_healthy(50, healthy_threshold=10) is True
    assert cd.is_blocked() is False


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
