"""GitHub rate-limit awareness.

Shared cooldown state for every :class:`GitHubClient` in the process.
Parses ``Retry-After`` + ``X-RateLimit-Reset`` on rate-limit responses,
records an absolute "do not call until" timestamp, and short-circuits
subsequent calls so agents don't burn more budget while the limit is
in effect.

Typed :class:`RateLimitError` (subclass of :class:`GitHubAPIError`)
lets callers catch rate-limit specifically and decide between
skipping the action, deferring to next cycle, or hard-failing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

logger = logging.getLogger(__name__)


_LOW_REMAINING_THRESHOLD = int(os.environ.get("CARETAKER_RL_LOW_THRESHOLD", "15"))
_SOFT_BACKOFF_SECONDS = float(os.environ.get("CARETAKER_RL_SOFT_BACKOFF_SECONDS", "2.0"))
_MAX_COOLDOWN_SECONDS = float(os.environ.get("CARETAKER_RL_MAX_COOLDOWN_SECONDS", "3600"))
# Threshold at which an active cooldown is considered stale and self-healed.
# When ``X-RateLimit-Remaining`` exceeds this on a fresh response while we're
# still in a cooldown window, the prior block came from a 403/429 whose reset
# has effectively elapsed (GitHub refilled the bucket) — keep blocking
# wastes time. Tuned well above the soft-throttle threshold so a single
# successful call doesn't repeatedly toggle the cooldown.
_HEALTHY_REMAINING_THRESHOLD = int(os.environ.get("CARETAKER_RL_HEALTHY_THRESHOLD", "100"))


class RateLimitCooldown:
    """Process-wide cooldown registry.

    Every :class:`GitHubClient` in the same caretaker run shares this
    singleton via :func:`get_cooldown`. When one request hits a 429 /
    403-rate-limit, the reset timestamp is stored here and every
    subsequent request across every client short-circuits until the
    window elapses. Fail-fast beats the alternative (burning 50 more
    calls into the same limit before the agent notices).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Unix epoch seconds past which it is safe to make a call again.
        self._blocked_until: float = 0.0
        # Last observed ``X-RateLimit-Remaining`` header value.
        self._last_remaining: int | None = None
        # Reason for the most recent block (short string, surfaced to callers).
        self._reason: str = ""

    # ── State queries ─────────────────────────────────────────────────

    def is_blocked(self, *, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        with self._lock:
            return now < self._blocked_until

    def seconds_remaining(self, *, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            return max(0.0, self._blocked_until - now)

    def snapshot(self) -> dict[str, float | int | str | None]:
        """Human-readable state (for logs / admin dashboard)."""
        with self._lock:
            return {
                "blocked_until": self._blocked_until,
                "seconds_remaining": max(0.0, self._blocked_until - time.time()),
                "last_remaining": self._last_remaining,
                "reason": self._reason,
            }

    # ── State mutations ───────────────────────────────────────────────

    def mark_blocked(self, until: float, *, reason: str = "") -> None:
        """Record a hard block until ``until`` (unix epoch seconds)."""
        capped = min(until, time.time() + _MAX_COOLDOWN_SECONDS)
        with self._lock:
            if capped > self._blocked_until:
                self._blocked_until = capped
                self._reason = reason or self._reason or "rate-limited"
                logger.warning(
                    "GitHub rate-limit cooldown engaged: blocked until %s "
                    "(%.0fs from now) reason=%s",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(capped)),
                    capped - time.time(),
                    self._reason,
                )

    def update_remaining(self, remaining: int) -> None:
        with self._lock:
            self._last_remaining = remaining

    def maybe_clear_if_healthy(
        self,
        current_remaining: int,
        *,
        healthy_threshold: int = _HEALTHY_REMAINING_THRESHOLD,
        now: float | None = None,
    ) -> bool:
        """Clear an active cooldown when the live rate-limit budget is healthy.

        Called from :func:`record_response_headers` after every successful
        response that carries ``X-RateLimit-Remaining``. Without this, a
        cooldown set by a single 403/429 with a far-future reset header
        would stick for up to :data:`_MAX_COOLDOWN_SECONDS` (1 hour by
        default) even after GitHub's bucket refilled — silently deferring
        every agent dispatch in that window.

        We only clear when:
        * we are currently blocked (``_blocked_until`` in the future), and
        * the freshly-observed ``current_remaining`` is well above the
          soft-throttle floor (``healthy_threshold``, default 100).

        Any subsequent rate-limit hit will re-engage the cooldown
        immediately via :func:`record_rate_limit_response`, so the worst
        case for a false-positive clear is one extra request before we
        re-block.

        Returns ``True`` when the cooldown was cleared.
        """
        now = now if now is not None else time.time()
        with self._lock:
            if self._blocked_until <= now:
                return False  # already expired naturally
            if current_remaining < healthy_threshold:
                return False
            previous_until = self._blocked_until
            previous_reason = self._reason
            self._blocked_until = 0.0
            self._reason = ""
        logger.info(
            "GitHub rate-limit cooldown self-healed: remaining=%d ≥ threshold=%d "
            "(would have blocked for another %.0fs, prior reason=%r)",
            current_remaining,
            healthy_threshold,
            previous_until - now,
            previous_reason,
        )
        return True

    def reset(self) -> None:
        """Test helper — clear all state."""
        with self._lock:
            self._blocked_until = 0.0
            self._last_remaining = None
            self._reason = ""


_COOLDOWN = RateLimitCooldown()


def get_cooldown() -> RateLimitCooldown:
    return _COOLDOWN


def reset_for_tests() -> None:
    _COOLDOWN.reset()


# ── Header parsing ────────────────────────────────────────────────────


def parse_rate_limit_headers(
    response: httpx.Response,
    *,
    now: float | None = None,
) -> tuple[float | None, int | None]:
    """Return ``(blocked_until_epoch | None, remaining | None)``.

    Honours ``Retry-After`` (seconds or HTTP-date), ``X-RateLimit-Reset``
    (unix seconds), and ``X-RateLimit-Remaining``. Takes the *later* of
    ``Retry-After``-derived and ``X-RateLimit-Reset`` timestamps so we
    respect whichever source says to wait longer.
    """
    now = now if now is not None else time.time()

    headers = response.headers

    blocked_candidates: list[float] = []

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after:
        try:
            delta = float(retry_after)
            blocked_candidates.append(now + delta)
        except (TypeError, ValueError):
            # HTTP-date format fallback.
            with contextlib.suppress(Exception):
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(retry_after)
                if dt is not None:
                    blocked_candidates.append(dt.timestamp())

    reset_ts = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if reset_ts:
        with contextlib.suppress(TypeError, ValueError):
            blocked_candidates.append(float(reset_ts))

    remaining_header = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    remaining: int | None = None
    if remaining_header:
        try:
            remaining = int(remaining_header)
        except (TypeError, ValueError):
            remaining = None

    blocked_until = max(blocked_candidates) if blocked_candidates else None
    return blocked_until, remaining


def record_response_headers(response: httpx.Response) -> None:
    """Update cooldown state from every response, rate-limited or not.

    Called on the success path so we notice budget exhaustion *before*
    the next request rather than after.

    Also self-heals a stale cooldown: if we're currently blocked but the
    response's ``X-RateLimit-Remaining`` shows the bucket has refilled,
    clear the cooldown so subsequent agent dispatches aren't gated by a
    timer that has effectively elapsed. See
    :meth:`RateLimitCooldown.maybe_clear_if_healthy`.
    """
    _blocked_until, remaining = parse_rate_limit_headers(response)
    if remaining is not None:
        _COOLDOWN.update_remaining(remaining)
        # Self-heal BEFORE the soft-throttle check so a healthy reading
        # clears a stale block before we'd consider re-engaging one.
        _COOLDOWN.maybe_clear_if_healthy(remaining)
        _publish_rate_limit_metrics()
        if remaining <= _LOW_REMAINING_THRESHOLD:
            # Soft throttle: add a small blocking window so bursts don't
            # blow the rest of the budget in one run.
            until = time.time() + _SOFT_BACKOFF_SECONDS
            _COOLDOWN.mark_blocked(
                until,
                reason=f"soft throttle: only {remaining} requests remain",
            )
            _publish_rate_limit_metrics()


def record_rate_limit_response(response: httpx.Response, *, status_code: int) -> float:
    """Record a 429 / 403-rate-limit response and return the cooldown expiry."""
    blocked_until, remaining = parse_rate_limit_headers(response)
    if remaining is not None:
        _COOLDOWN.update_remaining(remaining)
    # Fall back to a one-minute cushion when the response gives us
    # nothing to work with. GitHub's secondary rate-limits sometimes
    # omit the reset header entirely.
    until = blocked_until if blocked_until is not None else time.time() + 60.0
    _COOLDOWN.mark_blocked(
        until,
        reason=f"HTTP {status_code} rate-limited",
    )
    _publish_rate_limit_metrics()
    return until


def _publish_rate_limit_metrics() -> None:
    """Mirror the cooldown snapshot into the Prometheus gauges.

    The import is deferred so that the ``rate_limit`` module stays
    dependency-free when :mod:`caretaker.observability.metrics` is not
    yet importable (e.g. during partial test collection).
    """
    try:
        from caretaker.observability.metrics import (
            set_rate_limit_cooldown,
            set_rate_limit_remaining,
        )
    except Exception:  # pragma: no cover - observability must never cascade
        return
    try:
        snap = _COOLDOWN.snapshot()
        seconds_remaining = snap.get("seconds_remaining")
        if isinstance(seconds_remaining, int | float):
            set_rate_limit_cooldown("github", float(seconds_remaining))
        last_remaining = snap.get("last_remaining")
        if isinstance(last_remaining, int):
            set_rate_limit_remaining("github", last_remaining)
    except Exception:  # pragma: no cover
        pass


# ── Self-heal background task ─────────────────────────────────────────────


async def _default_http_get(url: str, *, token: str, timeout: float = 5.0) -> httpx.Response:
    """Perform a single GET against GitHub with the App's installation token.

    Extracted so tests can inject a stub. Production wiring uses this default.
    """
    import httpx as _httpx

    async with _httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )


def start_cooldown_self_heal_task(
    *,
    token_fetcher: Callable[[], Awaitable[str | None]],
    http_get: Callable[..., Awaitable[httpx.Response]] | None = None,
    interval_seconds: float = 60.0,
    rate_limit_url: str = "https://api.github.com/rate_limit",
) -> asyncio.Task[None]:
    """Start a background task that self-heals stuck cooldowns.

    GitHub's ``/rate_limit`` endpoint does NOT consume budget against the
    rate limit, so this task is safe to run frequently. It only fires a
    request when the cooldown is currently engaged; otherwise it's a
    no-op tick.

    The implementation is intentionally narrow:

    - On each tick, check ``_COOLDOWN.is_blocked()``. If False, skip.
    - Else fetch an installation token via ``token_fetcher`` (provided
      by the caller because token plumbing varies — tests inject a stub,
      production wires the App's token broker).
    - Send a single GET to ``rate_limit_url`` with the token. Pass the
      response through :func:`record_response_headers`. That call will
      invoke :meth:`RateLimitCooldown.maybe_clear_if_healthy` which is
      what actually clears the cooldown when the live bucket is healthy.

    All exceptions are caught and logged so a transient network failure
    or token mint hiccup never crashes the loop.

    Returns the :class:`asyncio.Task`. The caller is expected to keep a
    reference (else GC may cancel it) and cancel it in shutdown.
    """
    effective_http_get: Callable[..., Awaitable[httpx.Response]] = (
        http_get if http_get is not None else _default_http_get
    )

    async def _loop() -> None:
        while True:
            try:
                if _COOLDOWN.is_blocked():
                    token = await token_fetcher()
                    if token is None:
                        logger.debug(
                            "cooldown self-heal: no installation token available; "
                            "skipping this tick (cooldown remains engaged)"
                        )
                    else:
                        response = await effective_http_get(rate_limit_url, token=token)
                        record_response_headers(response)
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "cooldown self-heal iteration failed; will retry next tick",
                    exc_info=True,
                )
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_loop(), name="rate-limit-self-heal")


__all__ = [
    "RateLimitCooldown",
    "get_cooldown",
    "parse_rate_limit_headers",
    "record_rate_limit_response",
    "record_response_headers",
    "reset_for_tests",
    "start_cooldown_self_heal_task",
]
