"""RS256 JWT signer for GitHub App authentication.

GitHub Apps authenticate by minting a short-lived JWT (<= 10 min) signed
with the App's private key, then exchanging that JWT for a much shorter-
lived installation access token.  This module is the signer half.

We keep the dependency on ``PyJWT[crypto]`` optional so the rest of
caretaker (which does not need to mint JWTs) does not pay the install
cost.  Users who enable ``github_app.enabled = true`` must install
caretaker with the ``github-app`` extra.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# JWT spec says exp must be <= iat + 10 min.  We give ourselves 9 min of
# wall-clock lifetime and 60s of backwards skew to accommodate clock drift
# between the caretaker host and GitHub.
_JWT_LIFETIME_SECONDS = 9 * 60
_JWT_CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class _SignedJWT:
    token: str
    expires_at: int


class AppJWTSigner:
    """Mint JWTs for a GitHub App using its PEM-encoded private key.

    Instances are cheap and reusable.  The signer caches the most recently
    issued JWT for the majority of its useful life to avoid re-signing for
    every installation-token request.
    """

    def __init__(self, *, app_id: int, private_key_pem: str) -> None:
        if app_id <= 0:
            raise ValueError("app_id must be a positive integer")
        if not private_key_pem.strip():
            raise ValueError("private_key_pem must be a non-empty PEM string")
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._cached: _SignedJWT | None = None

    @property
    def app_id(self) -> int:
        return self._app_id

    def issue(self, *, now: int | None = None) -> str:
        """Return a valid App JWT, minting a new one when the cache is stale."""
        current = now if now is not None else int(time.time())
        cached = self._cached
        if cached is not None and cached.expires_at - _JWT_CLOCK_SKEW_SECONDS > current:
            return cached.token

        token = self._sign(current)
        self._cached = _SignedJWT(
            token=token,
            expires_at=current + _JWT_LIFETIME_SECONDS,
        )
        return token

    def _sign(self, now: int) -> str:
        try:
            import jwt as pyjwt
        except ImportError as exc:  # pragma: no cover — dep is optional
            raise RuntimeError(
                "PyJWT with the 'crypto' extra is required for GitHub App "
                "support.  Install caretaker with the 'github-app' extra, "
                "e.g. `pip install 'caretaker[github-app]'`."
            ) from exc

        payload = {
            "iat": now - _JWT_CLOCK_SKEW_SECONDS,
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": str(self._app_id),
        }
        return str(
            pyjwt.encode(
                payload,
                self._private_key_pem,
                algorithm="RS256",
            )
        )


__all__ = ["AppJWTSigner"]
