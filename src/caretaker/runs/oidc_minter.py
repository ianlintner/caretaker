"""Runner-side helper: mint a GitHub Actions OIDC JWT for the backend.

Inside a GitHub Actions job that has ``permissions: id-token: write``,
the runtime sets two environment variables:

* ``ACTIONS_ID_TOKEN_REQUEST_URL`` — endpoint to call for an OIDC JWT.
* ``ACTIONS_ID_TOKEN_REQUEST_TOKEN`` — short-lived bearer used to
  authenticate to that endpoint.

We exchange these for a JWT bound to the requested ``audience``, which
the backend then signature-validates via GitHub's JWKS. The audience
*must* match what the backend was configured with
(``CARETAKER_OIDC_GITHUB_AUDIENCE``).
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


_REQUEST_URL_ENV = "ACTIONS_ID_TOKEN_REQUEST_URL"
_REQUEST_TOKEN_ENV = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"


class OIDCMintError(RuntimeError):
    """Raised when an OIDC JWT cannot be minted."""


async def mint_actions_oidc_token(
    *,
    audience: str,
    timeout: float = 10.0,
) -> str:
    """Exchange the runner's request token for an OIDC JWT.

    Raises :class:`OIDCMintError` when the env vars are missing (likely
    because the workflow forgot ``permissions: id-token: write``) or the
    GitHub endpoint returns an error response.
    """
    url = os.environ.get(_REQUEST_URL_ENV, "").strip()
    token = os.environ.get(_REQUEST_TOKEN_ENV, "").strip()
    if not url or not token:
        raise OIDCMintError(
            "GitHub Actions OIDC env vars missing — workflow must declare "
            "'permissions: id-token: write' to mint an OIDC token."
        )

    params = {"audience": audience} if audience else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "caretaker-stream",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers=headers)
    if resp.status_code != 200:
        raise OIDCMintError(f"GitHub OIDC endpoint returned {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    jwt_token = data.get("value") if isinstance(data, dict) else None
    if not isinstance(jwt_token, str) or not jwt_token:
        raise OIDCMintError(f"OIDC endpoint returned unexpected payload: {data!r}")
    return jwt_token


__all__ = ["OIDCMintError", "mint_actions_oidc_token"]
