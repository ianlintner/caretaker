"""GitHub App implementation of :class:`GitHubCredentialsProvider`.

Resolves installation tokens for ``default_token`` and, when a
user-to-server token supplier is configured, delegated user tokens for
``copilot_token``.  If no user token supplier is configured
``copilot_token`` falls back to the installation token — on the
assumption that the Phase-1 spike documented in
``docs/github-app-spike.md`` may prove App-token Copilot assignment is
sufficient.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .installation_tokens import (
    InstallationTokenMinter,  # noqa: TC001 — used at runtime in __init__ signature
)

UserTokenSupplier = Callable[[int], Awaitable[str]]


class GitHubAppCredentialsProvider:
    """Mint installation (and optionally user-to-server) tokens on demand.

    Parameters
    ----------
    minter:
        The shared :class:`InstallationTokenMinter` to use for default
        (server-to-server) tokens.
    default_installation_id:
        Installation id to use when callers do not supply one explicitly.
        Optional; ``default_token`` / ``copilot_token`` will raise
        ``ValueError`` if no id is available at call time.
    user_token_supplier:
        Optional async callable that, given an installation id, returns a
        user-to-server token for that installation's authorizing user.
        Intended for the Copilot-assignment hand-off.
    """

    def __init__(
        self,
        *,
        minter: InstallationTokenMinter,
        default_installation_id: int | None = None,
        user_token_supplier: UserTokenSupplier | None = None,
    ) -> None:
        self._minter = minter
        self._default_installation_id = default_installation_id
        self._user_token_supplier = user_token_supplier

    def _resolve_installation_id(self, installation_id: int | None) -> int:
        resolved = installation_id or self._default_installation_id
        if resolved is None:
            raise ValueError(
                "no installation_id supplied and no default_installation_id configured"
            )
        return resolved

    async def default_token(self, *, installation_id: int | None = None) -> str:
        resolved = self._resolve_installation_id(installation_id)
        token = await self._minter.get_token(resolved)
        return token.token

    async def copilot_token(self, *, installation_id: int | None = None) -> str:
        resolved = self._resolve_installation_id(installation_id)
        if self._user_token_supplier is not None:
            user_token = await self._user_token_supplier(resolved)
            if user_token:
                return user_token
        # Fall back to the installation token; the caller (GitHubClient)
        # will surface any GitHub 403 if Copilot assignment still requires
        # a user identity.
        token = await self._minter.get_token(resolved)
        return token.token


__all__ = ["GitHubAppCredentialsProvider", "UserTokenSupplier"]
