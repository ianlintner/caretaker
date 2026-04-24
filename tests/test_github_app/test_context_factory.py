"""Tests for GitHubAppContextFactory."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from caretaker.github_app.context_factory import GitHubAppContextFactory, _split_repo
from caretaker.github_app.installation_tokens import InstallationToken
from caretaker.github_app.webhooks import ParsedWebhook


def _make_parsed(
    *,
    installation_id: int | None = 42,
    repository_full_name: str | None = "acme/widget",
    delivery: str = "d-0001",
) -> ParsedWebhook:
    return ParsedWebhook(
        event_type="pull_request",
        delivery_id=delivery,
        action="opened",
        installation_id=installation_id,
        repository_full_name=repository_full_name,
        payload={"action": "opened"},
    )


def _fake_token(installation_id: int = 42) -> InstallationToken:
    return InstallationToken(
        token="ghs_fake_token",
        expires_at=9_999_999_999,
        installation_id=installation_id,
    )


def _fake_minter(token: InstallationToken | None = None) -> MagicMock:
    minter = MagicMock()
    minter.get_token = AsyncMock(return_value=token or _fake_token())
    return minter


def _fake_llm_router() -> MagicMock:
    return MagicMock()


# ── _split_repo ───────────────────────────────────────────────────────


def test_split_repo_parses_owner_and_name() -> None:
    assert _split_repo("acme/widget", "d-0001") == ("acme", "widget")


def test_split_repo_handles_org_with_slash() -> None:
    # Only the first slash is the separator.
    owner, repo = _split_repo("acme/widget/v2", "d-0001")
    assert owner == "acme"
    assert repo == "widget/v2"


def test_split_repo_raises_on_missing_slash() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        _split_repo("acmewidget", "d-0001")


def test_split_repo_raises_on_none() -> None:
    with pytest.raises(ValueError, match="owner/repo"):
        _split_repo(None, "d-0001")


# ── GitHubAppContextFactory ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_mints_token_for_installation() -> None:
    minter = _fake_minter()
    factory = GitHubAppContextFactory(minter=minter, llm_router=_fake_llm_router())

    parsed = _make_parsed(installation_id=99)
    with _patch_client_no_config(factory):
        ctx = await factory.build(parsed)

    minter.get_token.assert_awaited_once_with(99)
    assert ctx.owner == "acme"
    assert ctx.repo == "widget"


@pytest.mark.asyncio
async def test_build_raises_when_installation_id_is_none() -> None:
    factory = GitHubAppContextFactory(minter=_fake_minter(), llm_router=_fake_llm_router())
    with pytest.raises(ValueError, match="installation_id is None"):
        await factory.build(_make_parsed(installation_id=None))


@pytest.mark.asyncio
async def test_build_uses_default_config_when_file_missing() -> None:
    from caretaker.config import MaintainerConfig

    default = MaintainerConfig()
    factory = GitHubAppContextFactory(
        minter=_fake_minter(),
        llm_router=_fake_llm_router(),
        default_config=default,
    )

    # get_file_contents returns None → file absent → fall back to default
    with _patch_client_returns(factory, file_content=None):
        ctx = await factory.build(_make_parsed())

    assert ctx.config is default


@pytest.mark.asyncio
async def test_build_loads_config_from_repo_when_present() -> None:
    from caretaker.config import MaintainerConfig

    cfg_yaml = yaml.dump({"version": "v1", "pr_agent": {"stuck_age_hours": 48}})
    encoded = base64.b64encode(cfg_yaml.encode()).decode()

    factory = GitHubAppContextFactory(minter=_fake_minter(), llm_router=_fake_llm_router())

    with _patch_client_returns(factory, file_content={"content": encoded}):
        ctx = await factory.build(_make_parsed())

    assert isinstance(ctx.config, MaintainerConfig)
    assert ctx.config.pr_agent.stuck_age_hours == 48


@pytest.mark.asyncio
async def test_build_falls_back_to_default_on_github_error() -> None:
    from caretaker.config import MaintainerConfig

    default = MaintainerConfig()
    factory = GitHubAppContextFactory(
        minter=_fake_minter(),
        llm_router=_fake_llm_router(),
        default_config=default,
    )

    with _patch_client_raises(factory):
        ctx = await factory.build(_make_parsed())

    assert ctx.config is default


@pytest.mark.asyncio
async def test_build_propagates_dry_run_flag() -> None:
    factory = GitHubAppContextFactory(
        minter=_fake_minter(), llm_router=_fake_llm_router(), dry_run=True
    )
    with _patch_client_no_config(factory):
        ctx = await factory.build(_make_parsed())
    assert ctx.dry_run is True


# ── context manager helpers ───────────────────────────────────────────


def _patch_client_no_config(factory: GitHubAppContextFactory):
    """Patch GitHubClient so get_file_contents returns None (no config file)."""
    return _patch_client_returns(factory, file_content=None)


def _patch_client_returns(factory: GitHubAppContextFactory, *, file_content):
    import unittest.mock as mock

    client_instance = MagicMock()
    client_instance.get_file_contents = AsyncMock(return_value=file_content)
    return mock.patch(
        "caretaker.github_app.context_factory.GitHubClient",
        return_value=client_instance,
    )


def _patch_client_raises(factory: GitHubAppContextFactory):
    import unittest.mock as mock

    client_instance = MagicMock()
    client_instance.get_file_contents = AsyncMock(side_effect=RuntimeError("network error"))
    return mock.patch(
        "caretaker.github_app.context_factory.GitHubClient",
        return_value=client_instance,
    )
