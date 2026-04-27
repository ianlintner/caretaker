"""Resolve ``owner/repo`` → GitHub App installation id.

Used by the runs API to verify that a workflow's OIDC token comes from a
repository that has installed the caretaker GitHub App. The resolver
holds a small async-safe in-memory cache (TTL-bound) so we don't slam
``GET /repos/{owner}/{repo}/installation`` on every webhook / log post.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from caretaker.github_app.jwt_signer import AppJWTSigner

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_TTL_SECONDS = 300  # 5 min — installations rarely change
_NEGATIVE_TTL_SECONDS = 60  # cache "not installed" briefly to absorb scans


@dataclass
class _CacheEntry:
    installation_id: int | None
    expires_at: float


class RepoInstallationResolver:
    """Resolve a repository full name to an App installation id.

    Returns ``None`` when the App is not installed on the repository
    (404 from GitHub) or the lookup fails for non-fatal reasons. Treat
    ``None`` as "not authorized" at every call site.
    """

    def __init__(
        self,
        *,
        signer: AppJWTSigner,
        http_client: httpx.AsyncClient | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        negative_ttl_seconds: int = _NEGATIVE_TTL_SECONDS,
    ) -> None:
        self._signer = signer
        self._http_client = http_client
        self._owns_client = http_client is None
        self._ttl = ttl_seconds
        self._neg_ttl = negative_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RepoInstallationResolver:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=15.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def get(self, repository: str) -> int | None:
        if not repository or "/" not in repository:
            return None
        key = repository.lower()
        now = time.monotonic()

        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached.expires_at > now:
                return cached.installation_id

        installation_id = await self._lookup(repository)
        ttl = self._ttl if installation_id is not None else self._neg_ttl
        async with self._lock:
            self._cache[key] = _CacheEntry(
                installation_id=installation_id,
                expires_at=now + ttl,
            )
        return installation_id

    async def invalidate(self, repository: str) -> None:
        async with self._lock:
            self._cache.pop(repository.lower(), None)

    async def _lookup(self, repository: str) -> int | None:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=15.0)
            self._owns_client = True

        owner, _, repo = repository.partition("/")
        if not owner or not repo:
            return None

        app_jwt = self._signer.issue()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            resp = await self._http_client.get(
                f"/repos/{owner}/{repo}/installation",
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("repo_installation lookup failed for %s: %s", repository, exc)
            return None

        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            logger.warning(
                "repo_installation lookup unexpected status %d for %s: %s",
                resp.status_code,
                repository,
                resp.text[:200],
            )
            return None
        data = resp.json()
        raw = data.get("id")
        if not isinstance(raw, int) or raw <= 0:
            return None
        return raw


__all__ = ["RepoInstallationResolver"]
