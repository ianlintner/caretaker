"""Tests for the per-repo MaintainerConfig cache.

The cache is the production hot-path for active webhook dispatch — every
delivery flows through it. Verify: cold-miss fetches, warm hits skip the
GitHub API call, schema-drift triggers re-fetch, invalidation works.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from caretaker.github_app.context_factory import (
    GitHubAppContextFactory,
    _ConfigCache,
)
from caretaker.github_app.installation_tokens import InstallationToken
from caretaker.github_app.webhooks import ParsedWebhook


def _parsed(delivery: str = "d-1") -> ParsedWebhook:
    return ParsedWebhook(
        event_type="pull_request",
        delivery_id=delivery,
        action="opened",
        installation_id=42,
        repository_full_name="acme/widget",
        payload={"action": "opened"},
    )


def _minter() -> MagicMock:
    m = MagicMock()
    m.get_token = AsyncMock(
        return_value=InstallationToken(token="ghs_x", expires_at=9_999_999_999, installation_id=42)
    )
    return m


def _client_with(file_content: dict | None) -> MagicMock:
    client = MagicMock()
    client.get_file_contents = AsyncMock(return_value=file_content)
    return client


@pytest.fixture()
def isolated_cache() -> _ConfigCache:
    """A cache with no Redis URL — purely in-process LRU."""
    return _ConfigCache(redis_url="")


@pytest.mark.asyncio
async def test_cache_warms_after_first_fetch(
    isolated_cache: _ConfigCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_yaml = yaml.dump({"version": "v1", "pr_agent": {"stuck_age_hours": 99}})
    encoded = base64.b64encode(cfg_yaml.encode()).decode()

    fake_client = _client_with({"content": encoded})
    monkeypatch.setattr(
        "caretaker.github_app.context_factory.GitHubClient",
        lambda token: fake_client,
    )

    factory = GitHubAppContextFactory(
        minter=_minter(), llm_router=MagicMock(), config_cache=isolated_cache
    )

    # First call: cache miss, hits the API.
    ctx1 = await factory.build(_parsed(delivery="d-1"))
    assert ctx1.config.pr_agent.stuck_age_hours == 99
    assert fake_client.get_file_contents.await_count == 1

    # Second call: cache hit, no second API call.
    ctx2 = await factory.build(_parsed(delivery="d-2"))
    assert ctx2.config.pr_agent.stuck_age_hours == 99
    assert fake_client.get_file_contents.await_count == 1


@pytest.mark.asyncio
async def test_cache_invalidate_forces_refetch(
    isolated_cache: _ConfigCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_yaml = yaml.dump({"version": "v1"})
    encoded = base64.b64encode(cfg_yaml.encode()).decode()
    fake_client = _client_with({"content": encoded})
    monkeypatch.setattr(
        "caretaker.github_app.context_factory.GitHubClient",
        lambda token: fake_client,
    )

    factory = GitHubAppContextFactory(
        minter=_minter(), llm_router=MagicMock(), config_cache=isolated_cache
    )

    await factory.build(_parsed(delivery="d-1"))
    await isolated_cache.invalidate("acme", "widget")
    await factory.build(_parsed(delivery="d-2"))

    assert fake_client.get_file_contents.await_count == 2


@pytest.mark.asyncio
async def test_cache_does_not_warm_on_missing_file(
    isolated_cache: _ConfigCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = _client_with(None)  # file does not exist
    monkeypatch.setattr(
        "caretaker.github_app.context_factory.GitHubClient",
        lambda token: fake_client,
    )

    factory = GitHubAppContextFactory(
        minter=_minter(), llm_router=MagicMock(), config_cache=isolated_cache
    )

    await factory.build(_parsed(delivery="d-1"))
    await factory.build(_parsed(delivery="d-2"))

    # We do not cache the absence of a file — both requests hit the API.
    # (Caching absence would mean a newly-onboarded repo has to wait for
    # cache TTL before its first config edit applies.)
    assert fake_client.get_file_contents.await_count == 2


@pytest.mark.asyncio
async def test_cache_falls_back_to_default_on_validation_error(
    isolated_cache: _ConfigCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage cached value triggers re-fetch + cache eviction."""
    # Pre-populate with a value that will fail Pydantic validation.
    await isolated_cache.set("acme", "widget", {"orchestrator": "not-a-dict"})

    cfg_yaml = yaml.dump({"version": "v1"})
    encoded = base64.b64encode(cfg_yaml.encode()).decode()
    fake_client = _client_with({"content": encoded})
    monkeypatch.setattr(
        "caretaker.github_app.context_factory.GitHubClient",
        lambda token: fake_client,
    )

    factory = GitHubAppContextFactory(
        minter=_minter(), llm_router=MagicMock(), config_cache=isolated_cache
    )

    ctx = await factory.build(_parsed())
    assert ctx.config.version == "v1"
    # Re-fetched from API after validation drop.
    assert fake_client.get_file_contents.await_count == 1
