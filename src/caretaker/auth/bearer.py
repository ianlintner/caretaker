"""Shared OAuth2 bearer-token verifier for caretaker backend resources.

This module provides a single canonical bearer-token verification path used by
all caretaker backend resources that accept service-to-service traffic
authenticated via OAuth2 access tokens (JWTs).  Currently consumed by the
fleet heartbeat endpoint; designed to be reused by future MCP/resource
endpoints so the entire backend has one auth path.

Usage
-----
At application startup (after reading config/env)::

    from caretaker.auth import bearer
    await bearer.configure(
        issuer_url="https://roauth2.cat-herding.net",
        required_scopes={"fleet:heartbeat"},
    )

In a route handler::

    from caretaker.auth.bearer import require_bearer_token, BearerPrincipal

    @router.post("/heartbeat")
    async def heartbeat(
        request: Request,
        principal: BearerPrincipal = Depends(require_bearer_token("fleet:heartbeat")),
    ) -> dict:
        ...

Validation rules
----------------
* The ``Authorization`` header MUST be ``Bearer <jwt>``.
* The JWT signature is validated against keys served at the issuer's
  ``jwks_uri`` (PyJWT ``PyJWKClient``).
* ``iss`` claim MUST equal the configured issuer URL.
* ``exp`` claim MUST be in the future.
* ``aud`` is **not** verified (the roauth2 server emits ``aud == client_id``,
  which is not a fixed audience we can pre-configure).
* ``scope`` claim MUST contain every required scope (space-separated string).
* When auth has not been configured (e.g. issuer URL not set in env),
  ``require_bearer_token`` raises HTTP 503 — fail-closed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable

import httpx
import jwt
from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BearerPrincipal:
    """Authenticated caller derived from a verified bearer JWT."""

    client_id: str
    scopes: frozenset[str]
    raw_claims: dict[str, Any] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


@dataclass
class _BearerAuthState:
    issuer_url: str
    jwks_uri: str
    jwk_client: jwt.PyJWKClient
    required_scopes: frozenset[str]
    leeway_seconds: int = 30


_state: _BearerAuthState | None = None
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def configure(
    *,
    issuer_url: str,
    required_scopes: Iterable[str] = (),
    discovery_timeout: float = 10.0,
    leeway_seconds: int = 30,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Initialise (or replace) the global bearer-auth state.

    Fetches the issuer's OIDC discovery document to learn the ``jwks_uri``
    and constructs a ``PyJWKClient`` (which caches keys with its own TTL).

    Raises ``RuntimeError`` if the discovery document cannot be loaded or is
    malformed; callers should treat that as a deployment misconfiguration.
    """
    issuer_url = issuer_url.rstrip("/")
    discovery_url = f"{issuer_url}/.well-known/openid-configuration"

    async def _fetch_metadata(client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.get(discovery_url, timeout=discovery_timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):  # pragma: no cover - defensive
            raise RuntimeError(f"OIDC discovery at {discovery_url} returned non-object")
        return data

    if http_client is None:
        async with httpx.AsyncClient(timeout=discovery_timeout) as client:
            metadata = await _fetch_metadata(client)
    else:
        metadata = await _fetch_metadata(http_client)

    discovered_issuer = str(metadata.get("issuer", "")).rstrip("/")
    if discovered_issuer != issuer_url:
        logger.warning(
            "OIDC issuer mismatch: configured=%s discovered=%s; using configured value",
            issuer_url,
            discovered_issuer,
        )

    jwks_uri = metadata.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise RuntimeError(f"OIDC discovery at {discovery_url} missing 'jwks_uri'")

    jwk_client = jwt.PyJWKClient(jwks_uri, cache_keys=True, lifespan=600)
    required = frozenset(s for s in required_scopes if s)

    new_state = _BearerAuthState(
        issuer_url=issuer_url,
        jwks_uri=jwks_uri,
        jwk_client=jwk_client,
        required_scopes=required,
        leeway_seconds=leeway_seconds,
    )

    with _state_lock:
        global _state
        _state = new_state

    logger.info(
        "Configured bearer-token auth: issuer=%s jwks_uri=%s required_scopes=%s",
        issuer_url,
        jwks_uri,
        sorted(required),
    )


def reset() -> None:
    """Clear the global state (for tests)."""
    with _state_lock:
        global _state
        _state = None


def is_configured() -> bool:
    return _state is not None


def get_state() -> _BearerAuthState | None:
    return _state


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": 'Bearer realm="caretaker"'},
        )
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer scheme",
            headers={"WWW-Authenticate": 'Bearer realm="caretaker"'},
        )
    token = parts[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token",
            headers={"WWW-Authenticate": 'Bearer realm="caretaker"'},
        )
    return token


def _scopes_from_claims(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope") or claims.get("scopes") or ""
    if isinstance(raw, str):
        return frozenset(s for s in raw.split() if s)
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw if s)
    return frozenset()


def _verify_token(state: _BearerAuthState, token: str) -> BearerPrincipal:
    try:
        signing_key = state.jwk_client.get_signing_key_from_jwt(token).key
    except jwt.exceptions.PyJWKClientError as exc:
        logger.warning("JWKS lookup failed for incoming token: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to resolve token signing key",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unexpected JWKS error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token signing key error",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=state.issuer_url,
            leeway=state.leeway_seconds,
            options={
                "require": ["exp", "iat", "iss"],
                "verify_aud": False,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc

    client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub") or ""
    scopes = _scopes_from_claims(claims)
    return BearerPrincipal(client_id=str(client_id), scopes=scopes, raw_claims=claims)


def _enforce_scopes(principal: BearerPrincipal, scopes: Iterable[str]) -> None:
    missing = [s for s in scopes if s and s not in principal.scopes]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token missing required scope(s): {', '.join(sorted(missing))}",
            headers={
                "WWW-Authenticate": (
                    f'Bearer error="insufficient_scope", scope="{" ".join(sorted(missing))}"'
                )
            },
        )


def require_bearer_token(
    *scopes: str,
) -> Callable[[Request], Coroutine[Any, Any, BearerPrincipal]]:
    """FastAPI dependency factory: returns a Depends-able callable.

    The returned callable validates the request's bearer token and ensures the
    JWT carries every scope in ``scopes`` (in addition to any scopes set
    globally via :func:`configure`).
    """

    extra_scopes = frozenset(s for s in scopes if s)

    async def _dependency(request: Request) -> BearerPrincipal:
        state = _state
        if state is None:
            logger.error(
                "Bearer auth used but not configured (issuer URL missing); rejecting request"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Bearer authentication not configured",
            )
        token = _extract_bearer_token(request)
        principal = _verify_token(state, token)
        _enforce_scopes(principal, state.required_scopes | extra_scopes)
        return principal

    return _dependency


__all__ = [
    "BearerPrincipal",
    "configure",
    "reset",
    "is_configured",
    "get_state",
    "require_bearer_token",
]
