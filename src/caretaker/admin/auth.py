"""OIDC authentication for the admin dashboard.

Implements the Authorization Code flow against an external OIDC provider
(e.g. rust-oauth2-server).  Sessions are stored in Redis and tracked via
a signed HTTP-only cookie.

Endpoints:
    GET  /api/auth/login    — redirect to OIDC provider
    GET  /api/auth/callback — exchange code for tokens, create session
    POST /api/auth/logout   — destroy session
    GET  /api/auth/me       — return current user info
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from caretaker.config import AdminDashboardConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Module-level singletons initialised by ``configure()``
# ---------------------------------------------------------------------------

_config: AdminDashboardConfig | None = None
_oidc_metadata: dict[str, Any] | None = None
_signer: Any = None  # itsdangerous.URLSafeTimedSerializer
_redis: Any = None  # redis.asyncio.Redis

SESSION_COOKIE = "caretaker_session"
_SESSION_PREFIX = "caretaker:admin:session:"


class UserInfo(BaseModel):
    sub: str
    email: str | None = None
    name: str | None = None
    picture: str | None = None


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


async def configure(config: AdminDashboardConfig) -> None:
    """Initialise the auth module.  Must be called at app startup."""
    global _config, _oidc_metadata, _signer, _redis  # noqa: PLW0603

    _config = config

    # Fetch OIDC discovery document
    import httpx

    discovery_url = config.oidc_issuer_url.rstrip("/")
    if not discovery_url.endswith("/.well-known/openid-configuration"):
        discovery_url += "/.well-known/openid-configuration"

    async with httpx.AsyncClient() as client:
        resp = await client.get(discovery_url)
        resp.raise_for_status()
        _oidc_metadata = resp.json()

    logger.info(
        "OIDC discovery loaded from %s (issuer=%s)",
        discovery_url,
        _oidc_metadata.get("issuer"),
    )

    # Session cookie signer
    from itsdangerous import URLSafeTimedSerializer

    session_secret = os.environ.get(config.session_secret_env, "")
    if not session_secret:
        logger.warning(
            "No session secret configured — generating ephemeral secret. "
            "Sessions will not survive restarts. Set the env var named by "
            "config.admin_dashboard.session_secret_env to fix."
        )
        session_secret = secrets.token_hex(32)

    _signer = URLSafeTimedSerializer(session_secret)

    # Redis for session storage
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(redis_url, decode_responses=True)
        logger.info("Admin session store: Redis")
    else:
        logger.warning("No REDIS_URL — using in-memory session store (single-replica only)")
        _redis = _InMemorySessionStore()


class _InMemorySessionStore:
    """Minimal Redis-compatible in-memory fallback for local dev."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


# ---------------------------------------------------------------------------
# Dependency: require authenticated session
# ---------------------------------------------------------------------------


async def require_session(request: Request) -> UserInfo:
    """FastAPI dependency that validates the session cookie and returns user info."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if _signer is None or _redis is None:
        raise HTTPException(status_code=503, detail="Auth not configured")

    try:
        sid = _signer.loads(session_id, max_age=_config.session_ttl_seconds if _config else 3600)
    except Exception as err:
        raise HTTPException(status_code=401, detail="Invalid or expired session") from err

    raw = await _redis.get(f"{_SESSION_PREFIX}{sid}")
    if not raw:
        raise HTTPException(status_code=401, detail="Session expired")

    return UserInfo.model_validate_json(raw)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Redirect the user to the OIDC provider's authorization endpoint."""
    if not _config or not _oidc_metadata:
        raise HTTPException(status_code=503, detail="Admin dashboard not configured")

    client_id = os.environ.get(_config.oidc_client_id_env, "")
    if not client_id:
        raise HTTPException(status_code=503, detail="OIDC client ID not configured")

    import base64
    import hashlib

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    if _redis:
        await _redis.set(f"{_SESSION_PREFIX}state:{state}", "1", ex=300)
        await _redis.set(f"{_SESSION_PREFIX}pkce:{state}", code_verifier, ex=300)

    base_url = (
        _config.public_base_url.rstrip("/")
        if _config.public_base_url
        else str(request.base_url).rstrip("/")
    )
    redirect_uri = f"{base_url}/api/auth/callback"

    auth_endpoint = _oidc_metadata["authorization_endpoint"]
    params = (
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid+email+profile"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    return RedirectResponse(url=f"{auth_endpoint}{params}")


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Handle the OIDC provider callback: exchange code for tokens, create session."""
    if error:
        raise HTTPException(status_code=400, detail=f"OIDC error: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    if not _config or not _oidc_metadata or not _signer or not _redis:
        raise HTTPException(status_code=503, detail="Auth not configured")

    # Validate CSRF state and retrieve PKCE verifier
    state_key = f"{_SESSION_PREFIX}state:{state}"
    pkce_key = f"{_SESSION_PREFIX}pkce:{state}"
    stored = await _redis.get(state_key)
    if not stored:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    code_verifier = await _redis.get(pkce_key)
    await _redis.delete(state_key)
    await _redis.delete(pkce_key)

    # Exchange code for tokens
    import httpx

    client_id = os.environ.get(_config.oidc_client_id_env, "")
    client_secret = os.environ.get(_config.oidc_client_secret_env, "")
    base_url = (
        _config.public_base_url.rstrip("/")
        if _config.public_base_url
        else str(request.base_url).rstrip("/")
    )
    redirect_uri = f"{base_url}/api/auth/callback"

    token_endpoint = _oidc_metadata["token_endpoint"]

    token_payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if code_verifier:
        token_payload["code_verifier"] = code_verifier

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            token_endpoint,
            data=token_payload,
            headers={"Accept": "application/json"},
        )

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s %s", token_resp.status_code, token_resp.text)
        raise HTTPException(status_code=502, detail="Token exchange failed")

    token_data = token_resp.json()

    # Decode ID token (basic validation — production should verify signature)
    id_token = token_data.get("id_token", "")
    user_info = await _extract_user_info(id_token, token_data)

    # Enforce email allowlist
    if _config.allowed_emails and user_info.email not in _config.allowed_emails:
        logger.warning("Login denied for email=%s (not in allowlist)", user_info.email)
        raise HTTPException(status_code=403, detail="Access denied")

    # Create session
    sid = str(uuid.uuid4())
    await _redis.set(
        f"{_SESSION_PREFIX}{sid}",
        user_info.model_dump_json(),
        ex=_config.session_ttl_seconds,
    )

    signed_sid = _signer.dumps(sid)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=signed_sid,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=_config.session_ttl_seconds,
        path="/",
    )

    logger.info("Login successful for %s (%s)", user_info.email, user_info.sub)
    return response


async def _extract_user_info(id_token: str, token_data: dict[str, Any]) -> UserInfo:
    """Extract user info from the ID token or userinfo endpoint."""
    # Try decoding JWT payload without signature verification (signature
    # was already verified by the OIDC provider during token exchange).
    if id_token:
        import base64

        parts = id_token.split(".")
        if len(parts) >= 2:
            # Pad base64
            payload = parts[1]
            payload += "=" * (4 - len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return UserInfo(
                sub=claims.get("sub", ""),
                email=claims.get("email"),
                name=claims.get("name"),
                picture=claims.get("picture"),
            )

    # Fallback: call userinfo endpoint
    if _oidc_metadata and "userinfo_endpoint" in _oidc_metadata:
        import httpx

        access_token = token_data.get("access_token", "")
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                _oidc_metadata["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return UserInfo(
                    sub=data.get("sub", ""),
                    email=data.get("email"),
                    name=data.get("name"),
                    picture=data.get("picture"),
                )

    raise HTTPException(status_code=502, detail="Could not extract user info")


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Destroy the session and clear the cookie."""
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id and _signer and _redis:
        try:
            sid = _signer.loads(session_id, max_age=86400)
            await _redis.delete(f"{_SESSION_PREFIX}{sid}")
        except Exception:
            pass

    response = JSONResponse(content={"status": "logged_out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
async def me(request: Request) -> UserInfo:
    """Return the current authenticated user."""
    return await require_session(request)
