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

import contextlib
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


_LOW_REMAINING_THRESHOLD = int(os.environ.get("CARETAKER_RL_LOW_THRESHOLD", "15"))
_SOFT_BACKOFF_SECONDS = float(os.environ.get("CARETAKER_RL_SOFT_BACKOFF_SECONDS", "2.0"))
_MAX_COOLDOWN_SECONDS = float(os.environ.get("CARETAKER_RL_MAX_COOLDOWN_SECONDS", "3600"))


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
    """
    _blocked_until, remaining = parse_rate_limit_headers(response)
    if remaining is not None:
        _COOLDOWN.update_remaining(remaining)
        if remaining <= _LOW_REMAINING_THRESHOLD:
            # Soft throttle: add a small blocking window so bursts don't
            # blow the rest of the budget in one run.
            until = time.time() + _SOFT_BACKOFF_SECONDS
            _COOLDOWN.mark_blocked(
                until,
                reason=f"soft throttle: only {remaining} requests remain",
            )


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
    return until


__all__ = [
    "RateLimitCooldown",
    "get_cooldown",
    "parse_rate_limit_headers",
    "record_rate_limit_response",
    "record_response_headers",
    "reset_for_tests",
]
