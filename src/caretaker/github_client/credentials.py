"""Pluggable credential providers for GitHub API access.

This is the Phase-0 abstraction described in ``docs/github-app-plan.md``.
The rest of caretaker gets its GitHub tokens through a
:class:`GitHubCredentialsProvider` rather than reading environment variables
directly, which lets the orchestrator run in four different modes without any
call-site changes:

1. **App mode** — installation tokens minted on demand from a JWT and cached.
2. **Delegated mode** — user-to-server tokens obtained from a maintainer who
   authorized the app (used for Copilot hand-off when the installation token
   is not sufficient).
3. **PAT mode** — the existing ``COPILOT_PAT`` flow.
4. **Actions mode** — falling back to ``GITHUB_TOKEN``.

The first two modes are implemented by ``GitHubAppCredentialsProvider`` in
``src/caretaker/github_app``; the last two are covered here by
``EnvCredentialsProvider`` which preserves current behavior exactly.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class GitHubCredentialsProvider(Protocol):
    """Async-friendly token source used by ``GitHubClient``.

    Implementations must return fresh, non-empty tokens or raise.  Callers
    must not cache the returned value beyond a single API request.
    """

    async def default_token(self, *, installation_id: int | None = None) -> str:
        """Return a token suitable for ordinary repo reads / writes."""

    async def copilot_token(self, *, installation_id: int | None = None) -> str:
        """Return a token suitable for the Copilot coding-agent hand-off flow.

        This may be identical to :meth:`default_token` when the caller's
        mode (e.g. App installation tokens, once GitHub allows it) can
        perform Copilot assignment directly.  Otherwise it should return a
        token attributed to a real user identity.
        """


class EnvCredentialsProvider:
    """The backward-compatible provider that reads ``GITHUB_TOKEN`` / ``COPILOT_PAT``.

    This preserves the exact semantics the :class:`GitHubClient` constructor
    had before the credentials-provider refactor: ``default_token`` prefers
    ``GITHUB_TOKEN`` (falling back to ``COPILOT_PAT``), and ``copilot_token``
    prefers ``COPILOT_PAT`` (falling back to ``GITHUB_TOKEN``).
    """

    def __init__(
        self,
        *,
        default_token: str | None = None,
        copilot_token: str | None = None,
    ) -> None:
        self._default = (
            default_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("COPILOT_PAT", "")
        )
        if not self._default:
            raise ValueError("GITHUB_TOKEN or COPILOT_PAT is required")
        self._copilot = copilot_token or os.environ.get("COPILOT_PAT") or self._default

    async def default_token(self, *, installation_id: int | None = None) -> str:
        return self._default

    async def copilot_token(self, *, installation_id: int | None = None) -> str:
        return self._copilot


class StaticCredentialsProvider:
    """Test helper that serves fixed tokens without touching the environment."""

    def __init__(self, *, default_token: str, copilot_token: str | None = None) -> None:
        if not default_token:
            raise ValueError("default_token must be non-empty")
        self._default = default_token
        self._copilot = copilot_token or default_token

    async def default_token(self, *, installation_id: int | None = None) -> str:
        return self._default

    async def copilot_token(self, *, installation_id: int | None = None) -> str:
        return self._copilot


class ChainCredentialsProvider:
    """Try a sequence of providers, returning the first one that yields a token.

    Useful for hybrid deployments: an App provider is tried first and an env
    provider is kept as a fallback so existing workflows keep working during
    a rollout.
    """

    def __init__(self, providers: list[GitHubCredentialsProvider]) -> None:
        if not providers:
            raise ValueError("ChainCredentialsProvider requires at least one provider")
        self._providers = providers

    async def default_token(self, *, installation_id: int | None = None) -> str:
        last_error: Exception | None = None
        for provider in self._providers:
            try:
                token = await provider.default_token(installation_id=installation_id)
                if token:
                    return token
            except Exception as exc:  # noqa: BLE001 — intentional fallthrough
                last_error = exc
        raise last_error or RuntimeError("no provider returned a default token")

    async def copilot_token(self, *, installation_id: int | None = None) -> str:
        last_error: Exception | None = None
        for provider in self._providers:
            try:
                token = await provider.copilot_token(installation_id=installation_id)
                if token:
                    return token
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise last_error or RuntimeError("no provider returned a copilot token")


__all__ = [
    "ChainCredentialsProvider",
    "EnvCredentialsProvider",
    "GitHubCredentialsProvider",
    "StaticCredentialsProvider",
]
