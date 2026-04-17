"""Tests for the GitHub credentials provider abstraction."""

from __future__ import annotations

import pytest

from caretaker.github_client.credentials import (
    ChainCredentialsProvider,
    EnvCredentialsProvider,
    GitHubCredentialsProvider,
    StaticCredentialsProvider,
)


def test_protocol_is_runtime_checkable() -> None:
    """GitHubCredentialsProvider should be detectable at runtime."""

    class ValidProvider:
        async def default_token(self, *, installation_id: int | None = None) -> str:
            return "tok1"

        async def copilot_token(self, *, installation_id: int | None = None) -> str:
            return "tok2"

    assert isinstance(ValidProvider(), GitHubCredentialsProvider)


# ── StaticCredentialsProvider ─────────────────────────────────────────


async def test_static_provider_returns_fixed_tokens() -> None:
    provider = StaticCredentialsProvider(
        default_token="default-tok",
        copilot_token="copilot-tok",
    )
    assert await provider.default_token() == "default-tok"
    assert await provider.copilot_token() == "copilot-tok"


async def test_static_provider_copilot_defaults_to_default() -> None:
    provider = StaticCredentialsProvider(default_token="only-one")
    assert await provider.copilot_token() == "only-one"


async def test_static_provider_rejects_empty_default() -> None:
    with pytest.raises(ValueError):
        StaticCredentialsProvider(default_token="")


# ── ChainCredentialsProvider ───────────────────────────────────────────


async def test_chain_returns_first_successful_token() -> None:
    # Use inline mocks so we can return "" without hitting StaticCredentialsProvider's guard.
    class EmptyProvider:
        async def default_token(self, *, installation_id: int | None = None) -> str:
            return ""

        async def copilot_token(self, *, installation_id: int | None = None) -> str:
            return ""

    chain = ChainCredentialsProvider(
        [
            EmptyProvider(),  # type: ignore[arg-type]
            StaticCredentialsProvider(default_token="fallback"),
        ]
    )
    assert await chain.default_token() == "fallback"


async def test_chain_falls_through_all_providers() -> None:
    class EmptyProvider:
        async def default_token(self, *, installation_id: int | None = None) -> str:
            return ""

        async def copilot_token(self, *, installation_id: int | None = None) -> str:
            return ""

    chain = ChainCredentialsProvider(
        [
            EmptyProvider(),  # type: ignore[arg-type]
            EmptyProvider(),  # type: ignore[arg-type]
        ]
    )
    with pytest.raises(RuntimeError, match="no provider returned"):
        await chain.default_token()


async def test_chain_respects_installation_id() -> None:
    """Installation id must be passed through to all providers."""

    class RecordingProvider:
        def __init__(self) -> None:
            self.calls: list[int | None] = []

        async def default_token(self, *, installation_id: int | None = None) -> str:
            self.calls.append(installation_id)
            return "tok"

        async def copilot_token(self, *, installation_id: int | None = None) -> str:
            self.calls.append(installation_id)
            return "tok"

    recorded = RecordingProvider()
    chain = ChainCredentialsProvider([recorded])
    await chain.default_token(installation_id=42)
    assert recorded.calls == [42]


# ── EnvCredentialsProvider ─────────────────────────────────────────────


async def test_env_provider_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gt_abc")
    monkeypatch.setenv("COPILOT_PAT", "cp_xyz")
    provider = EnvCredentialsProvider()
    assert await provider.default_token() == "gt_abc"
    assert await provider.copilot_token() == "cp_xyz"


async def test_env_provider_copilot_falls_back_to_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gt_abc")
    monkeypatch.delenv("COPILOT_PAT", raising=False)
    provider = EnvCredentialsProvider()
    assert await provider.copilot_token() == "gt_abc"


async def test_env_provider_raises_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_PAT", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN or COPILOT_PAT"):
        EnvCredentialsProvider()


async def test_env_provider_accepts_explicit_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env_default")
    monkeypatch.setenv("COPILOT_PAT", "env_copilot")
    provider = EnvCredentialsProvider(
        default_token="explicit_default",
        copilot_token="explicit_copilot",
    )
    assert await provider.default_token() == "explicit_default"
    assert await provider.copilot_token() == "explicit_copilot"
