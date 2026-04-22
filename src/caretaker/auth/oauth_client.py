"""OAuth2 ``client_credentials`` token client.

Fetches and caches access tokens for service-to-service calls. Designed
for CI use: caretaker consumer repos set ``OAUTH2_CLIENT_ID`` /
``OAUTH2_CLIENT_SECRET`` as repo secrets plus ``OAUTH2_TOKEN_URL`` as a
repo variable, and this module reads them via :func:`build_client_from_env`.

Concurrency: a single :class:`OAuth2ClientCredentials` instance may be
shared across tasks; access to the cached token is guarded by an
``asyncio.Lock`` so concurrent callers coalesce on a single refresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


# Refresh this many seconds before the server-reported expiry, so a token
# in flight never expires mid-request under clock skew.
_EXPIRY_SKEW_SECONDS = 30


class OAuth2TokenError(RuntimeError):
    """Raised when a token cannot be obtained from the authorization server."""


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    expires_at_monotonic: float

    def is_valid(self, skew: float = _EXPIRY_SKEW_SECONDS) -> bool:
        return time.monotonic() + skew < self.expires_at_monotonic


class OAuth2ClientCredentials:
    """Async OAuth2 client-credentials token fetcher with in-process cache.

    Parameters
    ----------
    client_id, client_secret:
        Credentials issued by the authorization server at client registration.
    token_url:
        Full token endpoint (e.g. ``https://auth.example.com/oauth/token``).
    scope:
        Optional space-separated list of scopes requested. When empty, the
        server's default scope set for the client is granted.
    timeout_seconds:
        Per-request HTTP timeout.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_url: str,
        scope: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        if not client_id or not client_secret or not token_url:
            raise ValueError("client_id, client_secret, and token_url are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope.strip()
        self._timeout = timeout_seconds
        self._lock = asyncio.Lock()
        self._cached: _CachedToken | None = None

    async def get_token(self, *, client: httpx.AsyncClient | None = None) -> str:
        """Return a valid access token, refreshing if cached one is stale."""
        async with self._lock:
            if self._cached is not None and self._cached.is_valid():
                return self._cached.access_token
            self._cached = await self._fetch_new(client=client)
            return self._cached.access_token

    async def authorization_header(
        self, *, client: httpx.AsyncClient | None = None
    ) -> dict[str, str]:
        """Convenience: return ``{"Authorization": "Bearer <token>"}``."""
        token = await self.get_token(client=client)
        return {"Authorization": f"Bearer {token}"}

    def invalidate(self) -> None:
        """Drop the cached token; the next call will fetch fresh."""
        self._cached = None

    async def _fetch_new(self, *, client: httpx.AsyncClient | None) -> _CachedToken:
        data = {"grant_type": "client_credentials"}
        if self._scope:
            data["scope"] = self._scope

        owns_client = client is None
        try:
            if owns_client:
                client = httpx.AsyncClient(timeout=self._timeout)
            assert client is not None
            try:
                resp = await client.post(
                    self._token_url,
                    data=data,
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                )
            finally:
                if owns_client:
                    await client.aclose()
        except httpx.HTTPError as exc:
            raise OAuth2TokenError(
                f"transport error fetching token from {self._token_url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise OAuth2TokenError(f"token endpoint returned {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise OAuth2TokenError(
                f"token endpoint returned non-JSON body: {resp.text[:200]}"
            ) from exc

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuth2TokenError("token endpoint response missing access_token")

        expires_in = payload.get("expires_in", 3600)
        try:
            expires_in_f = float(expires_in)
        except (TypeError, ValueError):
            expires_in_f = 3600.0
        # Clamp to a sane minimum to avoid pathological tight-loop refreshes
        # if the server returns 0 or a negative value.
        expires_in_f = max(expires_in_f, 60.0)

        return _CachedToken(
            access_token=access_token,
            expires_at_monotonic=time.monotonic() + expires_in_f,
        )


def build_client_from_env(
    *,
    client_id_env: str = "OAUTH2_CLIENT_ID",
    client_secret_env: str = "OAUTH2_CLIENT_SECRET",
    token_url_env: str = "OAUTH2_TOKEN_URL",
    scope_env: str = "OAUTH2_SCOPE",
    timeout_seconds: float = 10.0,
) -> OAuth2ClientCredentials | None:
    """Construct a client from process env vars, or ``None`` if unset.

    Returns ``None`` when any of the three required env vars is missing or
    empty — callers then fall back to their unauthenticated code path. This
    keeps OAuth2 strictly opt-in and preserves byte-identical behavior for
    consumers that haven't configured it yet.
    """
    client_id = os.environ.get(client_id_env, "").strip()
    client_secret = os.environ.get(client_secret_env, "").strip()
    token_url = os.environ.get(token_url_env, "").strip()
    scope = os.environ.get(scope_env, "").strip()
    if not (client_id and client_secret and token_url):
        logger.debug(
            "oauth2 client: skipping — required env vars for id/secret/token_url are unset"
        )
        return None
    return OAuth2ClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
        token_url=token_url,
        scope=scope,
        timeout_seconds=timeout_seconds,
    )
