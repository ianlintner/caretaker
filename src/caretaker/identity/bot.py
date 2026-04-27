"""Bot-login identity classification.

Single source of truth for "is this GitHub login an automation account?".
Consolidates the deterministic allowlist (well-known bot suffixes and named
bots such as ``copilot``, ``dependabot[bot]``, ``github-actions[bot]``) and
exposes an async :func:`classify_identity` helper that can fall back to a
memoised LLM lookup for unfamiliar logins when an :class:`~caretaker.llm.claude.ClaudeClient`
is supplied.

Design notes:

* :func:`is_automated` is a fast synchronous path. It uses *only* the
  deterministic allowlist — no I/O, no LLM call, no cache. Safe to call from
  hot paths and from synchronous code.
* :func:`classify_identity` returns a richer :class:`BotIdentity` payload
  with a ``family`` classification. For deterministic matches it never hits
  the LLM. For unknown logins it optionally consults the LLM and memoises the
  verdict in a process-level bounded TTL cache keyed by login.
* Both are goldfish-brain safe for empty strings — ``is_automated("")``
  returns ``False``. Passing ``None`` is a programmer error and raises
  :class:`TypeError` so upstream callers handle it explicitly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)


BotFamily = Literal[
    "github_bot",
    "copilot",
    "dependabot",
    "caretaker",
    "custom",
    "human",
]


# Explicit allowlist of well-known automation logins. Anything here is treated
# as automated even when the ``[bot]`` suffix is missing (e.g. ``copilot``).
# Keep in sync with the JS dispatch-guard regex in
# ``.github/workflows/maintainer.yml`` (handled separately by T-A2).
_NAMED_BOTS: frozenset[str] = frozenset(
    {
        "copilot",
        "dependabot",
        "dependabot[bot]",
        "dependabot-preview[bot]",
        "github-actions[bot]",
        "the-care-taker[bot]",
        "renovate[bot]",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
        "copilot[bot]",
        "github-copilot[bot]",
        "copilot-pull-request-reviewer",
        "github-advanced-security[bot]",
        "coderabbitai[bot]",
        "reviewdog[bot]",
        "sonarcloud[bot]",
    }
)

# Logins whose family is well-known to our deterministic classifier.
_FAMILY_BY_LOGIN: dict[str, BotFamily] = {
    "copilot": "copilot",
    "copilot[bot]": "copilot",
    "copilot-swe-agent": "copilot",
    "copilot-swe-agent[bot]": "copilot",
    "github-copilot[bot]": "copilot",
    "copilot-pull-request-reviewer": "copilot",
    "dependabot": "dependabot",
    "dependabot[bot]": "dependabot",
    "dependabot-preview[bot]": "dependabot",
    "the-care-taker[bot]": "caretaker",
    "github-actions[bot]": "github_bot",
    "renovate[bot]": "github_bot",
    "github-advanced-security[bot]": "github_bot",
    "coderabbitai[bot]": "github_bot",
    "reviewdog[bot]": "github_bot",
    "sonarcloud[bot]": "github_bot",
}


class BotIdentity(BaseModel):
    """Structured verdict for a GitHub login.

    Attributes:
        is_automated: ``True`` when the login belongs to a bot / automation
            account.
        family: Coarse category. ``None`` is not used by the public API —
            callers always see one of the :data:`BotFamily` literals.
        confidence: Classifier confidence in the ``[0, 1]`` range.
    """

    is_automated: bool
    family: BotFamily | None = None
    confidence: float = Field(ge=0.0, le=1.0)


def _deterministic_family(login: str) -> BotFamily | None:
    """Return the deterministic family for ``login`` if known, else ``None``."""
    fam = _FAMILY_BY_LOGIN.get(login)
    if fam is not None:
        return fam
    if login.endswith("[bot]"):
        return "github_bot"
    return None


def deterministic_family(login: str) -> BotFamily | None:
    """Return the deterministic family for *login*, or ``None`` if unknown.

    Synchronous, allocation-light. Returns one of :data:`BotFamily` for any
    login the classifier recognises; returns ``None`` when the login is
    outside the allowlist (callers may then decide to fall back to the LLM
    via :func:`classify_identity`).

    Args:
        login: GitHub login. Empty strings return ``None``.

    Raises:
        TypeError: If *login* is ``None`` (matches :func:`is_automated`).
    """
    if login is None:
        raise TypeError("deterministic_family(login) requires a string, got None")
    if not login:
        return None
    return _deterministic_family(login)


def is_automated(login: str) -> bool:
    """Return ``True`` when *login* belongs to a known automation account.

    Deterministic, synchronous, allocation-light. Never hits the network or
    the LLM. Safe to call from hot paths.

    Args:
        login: A GitHub login string (e.g. ``"dependabot[bot]"``,
            ``"alice"``). Empty strings are treated as non-automation.

    Returns:
        ``True`` if the login ends in ``[bot]`` or matches one of the
        well-known named automation accounts; ``False`` otherwise.

    Raises:
        TypeError: If *login* is ``None``. Callers are expected to handle
            unknown-user cases upstream rather than coerce ``None`` here.
    """
    if login is None:
        raise TypeError("is_automated(login) requires a string, got None")
    if not login:
        return False
    if login in _NAMED_BOTS:
        return True
    return login.endswith("[bot]")


# ── LLM fallback with TTL cache ──────────────────────────────────────────────

_CACHE_LOCK = threading.Lock()
_LLM_CACHE: dict[str, tuple[float, BotIdentity]] = {}
_DEFAULT_TTL_SECONDS: int = 86_400
_DEFAULT_CACHE_MAX_SIZE: int = 1_000


def _cache_get(login: str, *, ttl_seconds: int, now: float | None = None) -> BotIdentity | None:
    ts_now = time.monotonic() if now is None else now
    with _CACHE_LOCK:
        hit = _LLM_CACHE.get(login)
        if hit is None:
            return None
        inserted_at, value = hit
        if ts_now - inserted_at > ttl_seconds:
            # Expired — drop and miss.
            _LLM_CACHE.pop(login, None)
            return None
        return value


def _cache_put(
    login: str,
    value: BotIdentity,
    *,
    max_size: int,
    now: float | None = None,
) -> None:
    ts_now = time.monotonic() if now is None else now
    with _CACHE_LOCK:
        if len(_LLM_CACHE) >= max_size and login not in _LLM_CACHE:
            # Drop the oldest entry to stay under the bound. Python 3.7+ dicts
            # preserve insertion order, so ``next(iter(...))`` is the oldest.
            try:
                oldest = next(iter(_LLM_CACHE))
            except StopIteration:
                oldest = None
            if oldest is not None:
                _LLM_CACHE.pop(oldest, None)
        _LLM_CACHE[login] = (ts_now, value)


def _reset_cache_for_tests() -> None:
    """Clear the process-level LLM cache. Intended for tests only."""
    with _CACHE_LOCK:
        _LLM_CACHE.clear()


_LLM_PROMPT_TEMPLATE = (
    "{login} — is this a GitHub automation account? "
    'Respond with JSON: {{"is_automated": bool, "family": one of '
    '["github_bot", "copilot", "dependabot", "caretaker", '
    '"custom", "human"], "confidence": float in [0,1]}}.'
)


async def classify_identity(
    login: str,
    *,
    llm: ClaudeClient | None = None,
    llm_lookup_enabled: bool = False,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    cache_max_size: int = _DEFAULT_CACHE_MAX_SIZE,
) -> BotIdentity:
    """Classify a GitHub *login* into a :class:`BotIdentity`.

    Starts with the deterministic allowlist (same as :func:`is_automated`).
    If the deterministic path says "not a bot" *and* an LLM client is
    supplied *and* ``llm_lookup_enabled`` is ``True``, asks the model and
    memoises the verdict in a bounded TTL cache keyed by login.

    The LLM path is best-effort: any provider error, malformed JSON, or
    validation failure falls back to a human default and is *not* memoised
    (so a later successful call can refresh the cache).

    Args:
        login: GitHub login to classify.
        llm: Optional :class:`ClaudeClient` instance. Required to exercise
            the LLM path; ignored when deterministic says "bot" or when
            ``llm_lookup_enabled`` is ``False``.
        llm_lookup_enabled: Feature flag. When ``False`` (default) the
            function behaves identically to :func:`is_automated` — no LLM
            call and no cache.
        ttl_seconds: TTL for LLM-produced cache entries.
        cache_max_size: Hard upper bound on the cache entry count.

    Returns:
        A :class:`BotIdentity`. ``family`` is always one of the literals
        declared in :data:`BotFamily` for results returned from this
        function.

    Raises:
        TypeError: If *login* is ``None`` (matches :func:`is_automated`).
    """
    if login is None:
        raise TypeError("classify_identity(login) requires a string, got None")

    # Fast deterministic path. Everything known, bot or otherwise, short-circuits.
    if not login:
        return BotIdentity(is_automated=False, family="human", confidence=1.0)

    deterministic_fam = _deterministic_family(login)
    if deterministic_fam is not None:
        return BotIdentity(is_automated=True, family=deterministic_fam, confidence=1.0)

    if not llm_lookup_enabled or llm is None:
        return BotIdentity(is_automated=False, family="human", confidence=0.9)

    # Cache lookup before we consult the LLM.
    cached = _cache_get(login, ttl_seconds=ttl_seconds)
    if cached is not None:
        return cached

    try:
        if not getattr(llm, "available", True):
            return BotIdentity(is_automated=False, family="human", confidence=0.9)

        # We call ``structured_complete`` when available. Fall back to
        # ``complete`` + manual JSON parse for test doubles that only
        # implement the simpler surface.
        structured = getattr(llm, "structured_complete", None)
        verdict: BotIdentity
        if structured is not None:
            raw_verdict = await structured(
                _LLM_PROMPT_TEMPLATE.format(login=login),
                schema=BotIdentity,
                feature="bot_identity_lookup",
            )
            # ``structured_complete`` returns ``T`` (here, ``BotIdentity``)
            # but the attribute lookup is untyped, so re-validate to make
            # the guarantee explicit for both mypy and runtime callers.
            verdict = BotIdentity.model_validate(raw_verdict)
        else:
            raw = await llm.complete(
                "bot_identity_lookup",
                _LLM_PROMPT_TEMPLATE.format(login=login),
                max_tokens=200,
            )
            verdict = BotIdentity.model_validate(json.loads(raw))
    except Exception as exc:  # provider outage, bad JSON, schema mismatch
        logger.warning("classify_identity LLM fallback failed for %r: %s", login, exc)
        return BotIdentity(is_automated=False, family="human", confidence=0.9)

    _cache_put(login, verdict, max_size=cache_max_size)
    return verdict


__all__ = [
    "BotFamily",
    "BotIdentity",
    "classify_identity",
    "deterministic_family",
    "is_automated",
]
