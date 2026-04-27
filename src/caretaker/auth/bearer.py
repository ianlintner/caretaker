"""Shared OAuth2 / OIDC bearer-token verifier for caretaker backend resources.

This module provides the canonical bearer-token verification path used by all
caretaker backend resources that accept service-to-service traffic
authenticated via JWTs.  It supports multiple registered issuers (e.g. the
roauth2 fleet token issuer *and* GitHub Actions OIDC) by routing each incoming
token to the right verifier based on the token's ``iss`` claim.

Usage
-----
At application startup (after reading config/env)::

    from caretaker.auth import bearer

    # Existing roauth2-style issuer (audience left unverified):
    await bearer.configure(
        issuer_url="https://roauth2.cat-herding.net",
        required_scopes={"fleet:heartbeat"},
    )

    # Additional issuer (e.g. GitHub Actions OIDC) with audience pinning:
    await bearer.configure(
        issuer_url="https://token.actions.githubusercontent.com",
        audience="caretaker-backend",
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
* The token's ``iss`` claim MUST match a registered issuer.
* The JWT signature is validated against keys served at the issuer's
  ``jwks_uri`` (PyJWT ``PyJWKClient``).
* ``exp`` claim MUST be in the future (with configurable leeway).
* When the issuer was registered with an ``audience`` value, the token's
  ``aud`` claim MUST match it; otherwise ``aud`` is ignored.
* ``scope`` claim MUST contain every required scope (space-separated string).
* When no issuer has been registered, ``require_bearer_token`` raises HTTP
  503 — fail-closed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import jwt
from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable

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
    issuer: str = ""

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# Module-level state — registry keyed by issuer URL
# ---------------------------------------------------------------------------


@dataclass
class _BearerIssuerState:
    issuer_url: str
    jwks_uri: str
    jwk_client: jwt.PyJWKClient
    required_scopes: frozenset[str]
    audience: str | None = None
    leeway_seconds: int = 30


_issuers: dict[str, _BearerIssuerState] = {}
_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


async def configure(
    *,
    issuer_url: str,
    required_scopes: Iterable[str] = (),
    audience: str | None = None,
    discovery_timeout: float = 10.0,
    leeway_seconds: int = 30,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Register (or replace) a bearer-token issuer.

    Fetches the issuer's OIDC discovery document to learn the ``jwks_uri``
    and constructs a ``PyJWKClient`` (which caches keys with its own TTL).

    Calling :func:`configure` multiple times with different ``issuer_url``
    values registers each issuer independently — incoming tokens are routed
    to the correct verifier by their ``iss`` claim.  Calling it again with
    the same ``issuer_url`` replaces the existing registration.

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

    new_state = _BearerIssuerState(
        issuer_url=issuer_url,
        jwks_uri=jwks_uri,
        jwk_client=jwk_client,
        required_scopes=required,
        audience=audience,
        leeway_seconds=leeway_seconds,
    )

    with _state_lock:
        _issuers[issuer_url] = new_state

    logger.info(
        "Configured bearer-token issuer: issuer=%s jwks_uri=%s required_scopes=%s aud=%s",
        issuer_url,
        jwks_uri,
        sorted(required),
        audience or "<unverified>",
    )


def reset() -> None:
    """Clear all registered issuers (for tests)."""
    with _state_lock:
        _issuers.clear()


def is_configured() -> bool:
    """Return ``True`` when at least one issuer has been registered."""
    return bool(_issuers)


def is_issuer_configured(issuer_url: str) -> bool:
    return issuer_url.rstrip("/") in _issuers


def get_issuer_state(issuer_url: str) -> _BearerIssuerState | None:
    return _issuers.get(issuer_url.rstrip("/"))


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


def _peek_issuer(token: str) -> str:
    """Return the ``iss`` claim from an unverified JWT.

    Used only to route the token to the right verifier; the signature is
    re-verified against that issuer's JWKS afterwards, so an attacker cannot
    forge a token by lying about ``iss`` — they would have to also produce a
    valid signature against the matching JWKS.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Malformed token: {exc}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    iss = unverified.get("iss")
    if not isinstance(iss, str) or not iss:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'iss' claim",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return iss.rstrip("/")


def _verify_token(state: _BearerIssuerState, token: str) -> BearerPrincipal:
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

    decode_options: dict[str, Any] = {
        "require": ["exp", "iat", "iss"],
        "verify_aud": state.audience is not None,
    }

    decode_kwargs: dict[str, Any] = {
        "algorithms": ["RS256"],
        "issuer": state.issuer_url,
        "leeway": state.leeway_seconds,
        "options": decode_options,
    }
    if state.audience is not None:
        decode_kwargs["audience"] = state.audience

    try:
        claims = jwt.decode(token, signing_key, **decode_kwargs)
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
    except jwt.InvalidAudienceError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience",
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
    return BearerPrincipal(
        client_id=str(client_id),
        scopes=scopes,
        raw_claims=claims,
        issuer=state.issuer_url,
    )


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


def verify_token_string(token: str) -> BearerPrincipal:
    """Verify a raw token string and return the resolved principal.

    Used by code paths that receive a token outside of an HTTP header
    (e.g. SSE clients passing tokens via query string) and want the same
    multi-issuer routing + signature verification as the HTTP dependency.
    """
    if not _issuers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bearer authentication not configured",
        )
    iss = _peek_issuer(token)
    state = _issuers.get(iss)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unrecognized token issuer: {iss}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return _verify_token(state, token)


def require_bearer_token(
    *scopes: str,
) -> Callable[[Request], Coroutine[Any, Any, BearerPrincipal]]:
    """FastAPI dependency factory: returns a Depends-able callable.

    The returned callable validates the request's bearer token and ensures
    the JWT carries every scope in ``scopes`` plus any scopes set globally
    on the resolved issuer's registration.
    """

    extra_scopes = frozenset(s for s in scopes if s)

    async def _dependency(request: Request) -> BearerPrincipal:
        if not _issuers:
            logger.error(
                "Bearer auth used but no issuer configured; rejecting request",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Bearer authentication not configured",
            )
        token = _extract_bearer_token(request)
        iss = _peek_issuer(token)
        state = _issuers.get(iss)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unrecognized token issuer: {iss}",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        principal = _verify_token(state, token)
        _enforce_scopes(principal, state.required_scopes | extra_scopes)
        return principal

    return _dependency


__all__ = [
    "BearerPrincipal",
    "configure",
    "get_issuer_state",
    "is_configured",
    "is_issuer_configured",
    "require_bearer_token",
    "reset",
    "verify_token_string",
]
