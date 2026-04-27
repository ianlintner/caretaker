"""Installation-token minter and in-memory cache.

GitHub installation access tokens expire after 1 hour.  This module wraps
the ``POST /app/installations/{installation_id}/access_tokens`` call and
caches the returned token per installation, refreshing a configurable
window (default 5 min) before the advertised expiry.

The minter is *async* because caretaker's GitHub client and the webhook
receiver are async; the cache is process-local and intentionally small —
for multi-replica deployments the expected upgrade path is Redis, which
is already called out in ``docs/azure-mcp-architecture-plan.md`` as an
optional Phase-2 addition.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .jwt_signer import AppJWTSigner  # noqa: TC001 — used at runtime in __init__ signature

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_REFRESH_SKEW_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """A materialized installation access token and its expiry."""

    token: str
    expires_at: int  # Unix epoch seconds (UTC)
    installation_id: int

    def is_fresh(self, *, now: int, skew_seconds: int) -> bool:
        return self.expires_at - skew_seconds > now


class InstallationTokenCache:
    """Thread-safe (well, asyncio-safe) in-memory token cache."""

    def __init__(self) -> None:
        self._tokens: dict[int, InstallationToken] = {}
        self._lock = asyncio.Lock()

    async def get(self, installation_id: int) -> InstallationToken | None:
        async with self._lock:
            return self._tokens.get(installation_id)

    async def put(self, token: InstallationToken) -> None:
        async with self._lock:
            self._tokens[token.installation_id] = token

    async def invalidate(self, installation_id: int) -> None:
        async with self._lock:
            self._tokens.pop(installation_id, None)

    async def clear(self) -> None:
        async with self._lock:
            self._tokens.clear()


def _parse_expiry(value: str) -> int:
    """Parse a GitHub ISO8601 ``expires_at`` string into a UTC epoch second."""
    # GitHub returns e.g. "2026-04-17T00:00:00Z".  ``fromisoformat`` accepts
    # "+00:00" only, so swap the terminal Z.
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"unparseable installation-token expires_at: {value!r}") from exc
    return int(dt.timestamp())


class InstallationTokenMinter:
    """Mint installation tokens via the GitHub App API.

    Parameters
    ----------
    signer:
        The shared :class:`AppJWTSigner` for the caretaker App.
    cache:
        Optional cache instance; a fresh one is created if not provided.
    refresh_skew_seconds:
        Treat tokens within this window of expiry as stale and re-mint.
    http_client:
        An injected ``httpx.AsyncClient``.  The minter takes ownership of
        its lifecycle only when it constructs its own client (via
        :meth:`__aenter__`).  This split makes testing with ``respx`` and
        reusing a pooled client from the webhook service both ergonomic.
    """

    def __init__(
        self,
        *,
        signer: AppJWTSigner,
        cache: InstallationTokenCache | None = None,
        refresh_skew_seconds: int = _DEFAULT_REFRESH_SKEW_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if refresh_skew_seconds < 0:
            raise ValueError("refresh_skew_seconds must be >= 0")
        self._signer = signer
        self._cache = cache or InstallationTokenCache()
        self._refresh_skew = refresh_skew_seconds
        self._client = http_client
        self._owns_client = http_client is None

    async def __aenter__(self) -> InstallationTokenMinter:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=30.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_token(
        self,
        installation_id: int,
        *,
        now: int | None = None,
    ) -> InstallationToken:
        """Return a fresh installation token, using the cache when possible."""
        if installation_id <= 0:
            raise ValueError("installation_id must be a positive integer")

        current = now if now is not None else int(time.time())
        cached = await self._cache.get(installation_id)
        if cached is not None and cached.is_fresh(now=current, skew_seconds=self._refresh_skew):
            return cached

        minted = await self._mint(installation_id=installation_id)
        await self._cache.put(minted)
        return minted

    async def invalidate(self, installation_id: int) -> None:
        await self._cache.invalidate(installation_id)

    async def _mint(self, *, installation_id: int) -> InstallationToken:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=30.0)
            self._owns_client = True

        app_jwt = self._signer.issue()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        resp = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=headers,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"failed to mint installation token for "
                f"installation_id={installation_id}: "
                f"{resp.status_code} {resp.text}"
            )

        data: dict[str, Any] = resp.json()
        token = data.get("token")
        expires_at_raw = data.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at_raw, str):
            raise RuntimeError(f"malformed installation-token response from GitHub: {data!r}")
        expires_at = _parse_expiry(expires_at_raw)
        logger.info(
            "minted installation token for installation_id=%d (expires in %ds)",
            installation_id,
            max(expires_at - int(time.time()), 0),
        )
        return InstallationToken(
            token=token,
            expires_at=expires_at,
            installation_id=installation_id,
        )


__all__ = [
    "InstallationToken",
    "InstallationTokenCache",
    "InstallationTokenMinter",
]
