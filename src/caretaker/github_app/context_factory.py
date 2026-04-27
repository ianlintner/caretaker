"""Concrete AgentContextFactory for GitHub App webhook dispatch.

Wires together the installation token minter, GitHubClient construction,
per-repo MaintainerConfig fetching (Redis-cached), and LLMRouter so the
dispatcher never needs to know about any of these.

Per-repo config caching
-----------------------

Active dispatch fires for every webhook delivery. Without caching, every
delivery would issue an extra ``GET /repos/{owner}/{repo}/contents/.github/maintainer/config.yml``
just to discover the agent settings — burning the App's secondary rate
limit and adding ~150ms of latency to every dispatch. We cache the
parsed :class:`MaintainerConfig` in Redis keyed on
``(owner, repo)`` with a short TTL (default 5 min). Fleet-wide config
changes propagate within the TTL; in practice they happen via PR merge,
which is itself a webhook → cache stays warm but the next push refreshes
naturally on the new owner/repo deliveries.

In-process LRU is the fallback when Redis is unavailable, so single-pod
dev keeps working.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import yaml

from caretaker.agent_protocol import AgentContext
from caretaker.config import MaintainerConfig
from caretaker.github_client.api import GitHubClient

if TYPE_CHECKING:
    import redis.asyncio

    from caretaker.github_app.installation_tokens import InstallationTokenMinter
    from caretaker.github_app.webhooks import ParsedWebhook
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_CONFIG_PATH = ".github/maintainer/config.yml"
_DEFAULT_CACHE_TTL_SECONDS = 300  # 5 min — propagation window for fleet-wide config edits
_DEFAULT_LRU_CAPACITY = 256  # in-process fallback


class _ConfigCache:
    """Two-tier cache: Redis-shared with in-process LRU fallback.

    The two tiers are deliberately not stacked (no read-through). On a
    miss, the loader is invoked exactly once thanks to a per-key
    ``asyncio.Lock`` so concurrent webhooks for the same repo collapse
    to a single Contents API call.
    """

    def __init__(
        self,
        *,
        redis_url: str = "",
        ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        lru_capacity: int = _DEFAULT_LRU_CAPACITY,
        key_prefix: str = "caretaker:config:",
    ) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._lru: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lru_capacity = lru_capacity
        self._key_prefix = key_prefix
        self._client: redis.asyncio.Redis[str] | None = None
        self._connect_lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def _redis(self) -> redis.asyncio.Redis[str] | None:
        if not self._redis_url:
            return None
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is None:
                try:
                    import redis.asyncio as aioredis

                    self._client = aioredis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_connect_timeout=5,
                        socket_timeout=5,
                    )
                except Exception:
                    logger.warning(
                        "config cache: Redis unavailable; falling back to in-process LRU",
                        exc_info=True,
                    )
                    return None
        return self._client

    def _key(self, owner: str, repo: str) -> str:
        return f"{self._key_prefix}{owner}/{repo}"

    def _key_lock(self, key: str) -> asyncio.Lock:
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    async def get(self, owner: str, repo: str) -> dict[str, Any] | None:
        key = self._key(owner, repo)

        # Tier 1: Redis (shared across replicas).
        client = await self._redis()
        if client is not None:
            try:
                raw = await client.get(key)
                if raw is not None:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
            except Exception:
                logger.debug("config cache: Redis GET failed", exc_info=True)
                self._client = None  # force reconnect

        # Tier 2: in-process LRU.
        cached = self._lru.get(key)
        if cached is not None:
            self._lru.move_to_end(key)
            return cached

        return None

    async def set(self, owner: str, repo: str, raw_config: dict[str, Any]) -> None:
        key = self._key(owner, repo)

        # Always populate the local LRU so a subsequent Redis outage still
        # serves the warm value.
        self._lru[key] = raw_config
        self._lru.move_to_end(key)
        while len(self._lru) > self._lru_capacity:
            self._lru.popitem(last=False)

        client = await self._redis()
        if client is not None:
            try:
                await client.set(
                    key,
                    json.dumps(raw_config, default=str),
                    ex=self._ttl_seconds,
                )
            except Exception:
                logger.debug("config cache: Redis SET failed", exc_info=True)
                self._client = None

    async def invalidate(self, owner: str, repo: str) -> None:
        key = self._key(owner, repo)
        self._lru.pop(key, None)
        client = await self._redis()
        if client is not None:
            try:
                await client.delete(key)
            except Exception:
                logger.debug("config cache: Redis DEL failed", exc_info=True)


_default_cache: _ConfigCache | None = None


def _get_default_cache() -> _ConfigCache:
    global _default_cache  # noqa: PLW0603
    if _default_cache is None:
        _default_cache = _ConfigCache(
            redis_url=os.environ.get("REDIS_URL", "").strip(),
            ttl_seconds=int(
                os.environ.get("CARETAKER_CONFIG_CACHE_TTL_SECONDS", "")
                or _DEFAULT_CACHE_TTL_SECONDS
            ),
        )
    return _default_cache


def reset_config_cache() -> None:
    """Test hook: drop the module-level cache singleton."""
    global _default_cache  # noqa: PLW0603
    _default_cache = None


class GitHubAppContextFactory:
    """Build an :class:`AgentContext` for each incoming webhook delivery.

    One instance is created at backend startup and shared across all
    deliveries (it is stateless beyond the injected collaborators).

    Args:
        minter: The installation token minter. Must not be ``None`` — callers
            should verify the GitHub App is configured before constructing this.
        llm_router: Shared LLM router built from the backend's own config.
        default_config: Fallback ``MaintainerConfig`` used when the target
            repo has no ``.github/maintainer/config.yml``. Defaults to an
            all-defaults instance.
        dry_run: When ``True`` all constructed :class:`AgentContext` instances
            have ``dry_run=True`` so agents skip mutating API calls.
        config_cache: Optional shared cache. Defaults to a module-level
            singleton wired to ``REDIS_URL``.
    """

    def __init__(
        self,
        *,
        minter: InstallationTokenMinter,
        llm_router: LLMRouter,
        default_config: MaintainerConfig | None = None,
        dry_run: bool = False,
        config_cache: _ConfigCache | None = None,
    ) -> None:
        self._minter = minter
        self._llm_router = llm_router
        self._default_config = default_config or MaintainerConfig()
        self._dry_run = dry_run
        self._cache = config_cache or _get_default_cache()

    async def build(self, parsed: ParsedWebhook) -> AgentContext:
        """Mint a token, construct a client, and load the repo config."""
        if parsed.installation_id is None:
            raise ValueError(
                f"delivery {parsed.delivery_id}: installation_id is None — "
                "cannot mint installation token for anonymous deliveries"
            )

        token = await self._minter.get_token(parsed.installation_id)
        client = GitHubClient(token=token.token)

        owner, repo = _split_repo(parsed.repository_full_name, parsed.delivery_id)
        config = await self._load_config(client, owner, repo)

        logger.debug(
            "built AgentContext owner=%s repo=%s installation=%s delivery=%s",
            owner,
            repo,
            parsed.installation_id,
            parsed.delivery_id,
        )
        return AgentContext(
            github=client,
            owner=owner,
            repo=repo,
            config=config,
            llm_router=self._llm_router,
            dry_run=self._dry_run,
        )

    async def _load_config(self, client: GitHubClient, owner: str, repo: str) -> MaintainerConfig:
        """Fetch the repo's maintainer config or return the default.

        Cache lookup before API call; cache populate after successful
        validation. We cache the *raw dict*, not the validated
        :class:`MaintainerConfig`, so schema migrations do not require a
        cache flush — every read re-validates against the current Pydantic
        model.
        """
        cached = await self._cache.get(owner, repo)
        if cached is not None:
            try:
                return MaintainerConfig.model_validate(cached)
            except Exception:
                # Schema drift between cached and current config model.
                # Drop the entry and re-fetch.
                logger.info("config cache: validation failed for %s/%s; invalidating", owner, repo)
                await self._cache.invalidate(owner, repo)

        try:
            raw = await client.get_file_contents(owner, repo, _CONFIG_PATH)
            if raw is None:
                logger.debug(
                    "no maintainer config at %s in %s/%s; using defaults",
                    _CONFIG_PATH,
                    owner,
                    repo,
                )
                return self._default_config

            content_b64: str = raw.get("content", "")
            content_bytes = base64.b64decode(content_b64.replace("\n", ""))
            data = yaml.safe_load(content_bytes.decode()) or {}
            config = MaintainerConfig.model_validate(data)
            await self._cache.set(owner, repo, data)
            return config
        except Exception:
            logger.warning(
                "failed to load maintainer config from %s/%s; using defaults",
                owner,
                repo,
                exc_info=True,
            )
            return self._default_config


def _split_repo(repository_full_name: str | None, delivery_id: str) -> tuple[str, str]:
    """Split ``owner/repo`` into ``(owner, repo)``, raising on bad input."""
    if not repository_full_name or "/" not in repository_full_name:
        raise ValueError(
            f"delivery {delivery_id}: repository_full_name {repository_full_name!r} "
            "is not in 'owner/repo' format"
        )
    owner, _, repo = repository_full_name.partition("/")
    return owner, repo


__all__ = ["GitHubAppContextFactory", "reset_config_cache"]
