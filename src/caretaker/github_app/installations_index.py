"""Enumerate every installation of the caretaker GitHub App and the repos under it.

The reconciliation scheduler needs the full fleet to fan out periodic
runs. Heartbeat-based discovery (the legacy fleet registry) is no longer
sufficient because we're removing client-side heartbeats — instead we
ask GitHub directly via the App JWT.

API path
--------

1. ``GET /app/installations`` (App JWT) — list every installation of
   the App. Pagination via ``page`` query param.
2. For each installation: mint a short-lived installation token via
   :class:`InstallationTokenMinter`, then
   ``GET /installation/repositories`` (token) — list the repos that
   installation has access to.

We cache the index in-process for a configurable TTL so a 30-minute
reconciliation cron does not call ``/app/installations`` 30 times per
minute when the scheduler ticks past quickly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from caretaker.github_app.installation_tokens import InstallationTokenMinter
    from caretaker.github_app.jwt_signer import AppJWTSigner

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_TTL_SECONDS = 600  # 10 min — installations / repo lists rarely change


@dataclass(frozen=True, slots=True)
class FleetRepo:
    owner: str
    repo: str
    installation_id: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


class InstallationsIndex:
    """Snapshot the App's installation graph.

    The index is best-effort — a partial result (some installations
    enumerated, others timed out) is still returned. The scheduler
    treats it as a fan-out target list, so missing one installation on
    one tick is recoverable on the next.
    """

    def __init__(
        self,
        *,
        signer: AppJWTSigner,
        token_minter: InstallationTokenMinter,
        http_client: httpx.AsyncClient | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._signer = signer
        self._token_minter = token_minter
        self._http_client = http_client
        self._owns_client = http_client is None
        self._ttl = ttl_seconds
        self._cache: list[FleetRepo] | None = None
        self._cache_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> InstallationsIndex:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=15.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def list_repos(self, *, force_refresh: bool = False) -> list[FleetRepo]:
        now = time.monotonic()
        async with self._lock:
            if not force_refresh and self._cache is not None and self._cache_expires_at > now:
                return list(self._cache)

        repos = await self._fetch_all()

        async with self._lock:
            self._cache = list(repos)
            self._cache_expires_at = time.monotonic() + self._ttl
        return repos

    async def _fetch_all(self) -> list[FleetRepo]:
        installations = await self._list_installations()
        results: list[FleetRepo] = []
        for installation_id in installations:
            try:
                installation_repos = await self._list_installation_repos(installation_id)
            except Exception:
                logger.warning(
                    "installations_index: failed to list repos for installation %d",
                    installation_id,
                    exc_info=True,
                )
                continue
            for owner, repo in installation_repos:
                results.append(FleetRepo(owner=owner, repo=repo, installation_id=installation_id))
        return results

    async def _list_installations(self) -> list[int]:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=15.0)
            self._owns_client = True
        app_jwt = self._signer.issue()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        installations: list[int] = []
        page = 1
        while True:
            try:
                resp = await self._http_client.get(
                    "/app/installations",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
            except httpx.HTTPError:
                logger.warning(
                    "installations_index: /app/installations request failed", exc_info=True
                )
                break
            if resp.status_code >= 400:
                logger.warning(
                    "installations_index: /app/installations status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                break
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            for entry in data:
                inst_id = entry.get("id")
                if isinstance(inst_id, int) and inst_id > 0:
                    installations.append(inst_id)
            if len(data) < 100:
                break
            page += 1
        return installations

    async def _list_installation_repos(self, installation_id: int) -> list[tuple[str, str]]:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(base_url=_GITHUB_API, timeout=15.0)
            self._owns_client = True
        token = await self._token_minter.get_token(installation_id)
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        out: list[tuple[str, str]] = []
        page = 1
        while True:
            try:
                resp = await self._http_client.get(
                    "/installation/repositories",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
            except httpx.HTTPError:
                break
            if resp.status_code >= 400:
                break
            data = resp.json()
            repos = data.get("repositories", []) if isinstance(data, dict) else []
            if not repos:
                break
            for entry in repos:
                full = entry.get("full_name", "")
                if "/" not in full:
                    continue
                owner, _, repo = full.partition("/")
                out.append((owner, repo))
            if len(repos) < 100:
                break
            page += 1
        return out


__all__ = ["FleetRepo", "InstallationsIndex"]
