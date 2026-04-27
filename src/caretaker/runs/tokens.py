"""HMAC-signed ``ingest_token`` issuance.

After ``POST /runs/start`` validates the runner's OIDC token, we hand the
shipper a short-lived ``ingest_token`` bound to the backend ``run_id``.
The shipper presents this token (instead of re-minting OIDC each chunk)
on every subsequent ``/logs``, ``/heartbeat``, ``/finish``, and SSE-tail
call. Tokens are signed with a backend-side HMAC secret so verification
needs no JWKS round-trip and no DB read.

Token format: ``v1.<run_id>.<expires_unix>.<purpose>.<base64-hmac>``.

We do not store issued tokens — verification is purely cryptographic.
A leaked token is bounded by the ``run_id`` it embeds and the ``exp``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


_DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6h — typical max GitHub Actions job duration
_TOKEN_VERSION = "v1"


class IngestPurpose(StrEnum):
    """Bounded purpose tags (also part of the HMAC input)."""

    LOGS = "logs"
    HEARTBEAT = "heartbeat"
    FINISH = "finish"
    TAIL = "tail"
    ANY = "any"  # used by the shipper to cover all of the above


@dataclass(frozen=True)
class IngestPrincipal:
    run_id: str
    purpose: IngestPurpose
    expires_at: int


class IngestTokenError(Exception):
    """Token failed validation."""


def _secret() -> bytes:
    raw = os.environ.get("CARETAKER_RUNS_INGEST_TOKEN_SECRET", "").strip()
    if not raw:
        raise IngestTokenError(
            "CARETAKER_RUNS_INGEST_TOKEN_SECRET not configured; cannot sign or verify "
            "ingest tokens. Set it to a random 32+ byte secret."
        )
    return raw.encode("utf-8")


def _sign(payload: str) -> str:
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def issue(
    *,
    run_id: str,
    purpose: IngestPurpose = IngestPurpose.ANY,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint an ``ingest_token`` for the given ``run_id``."""
    if not run_id:
        raise ValueError("run_id required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    issued_at = now if now is not None else int(time.time())
    expires_at = issued_at + ttl_seconds
    payload = f"{_TOKEN_VERSION}.{run_id}.{expires_at}.{purpose.value}"
    return f"{payload}.{_sign(payload)}"


def verify(
    token: str,
    *,
    run_id: str,
    require_purpose: IngestPurpose | None = None,
    now: int | None = None,
) -> IngestPrincipal:
    """Verify a token; return the resolved :class:`IngestPrincipal`.

    Raises :class:`IngestTokenError` on any validation failure.
    """
    if not token:
        raise IngestTokenError("missing ingest token")
    parts = token.split(".")
    if len(parts) != 5:
        raise IngestTokenError("malformed ingest token")
    version, token_run_id, expires_str, purpose_str, signature = parts
    if version != _TOKEN_VERSION:
        raise IngestTokenError("unsupported ingest token version")
    if token_run_id != run_id:
        raise IngestTokenError("ingest token bound to a different run")
    try:
        expires_at = int(expires_str)
    except ValueError as exc:
        raise IngestTokenError("ingest token has malformed expiry") from exc
    try:
        purpose = IngestPurpose(purpose_str)
    except ValueError as exc:
        raise IngestTokenError("ingest token has malformed purpose") from exc

    payload = f"{version}.{token_run_id}.{expires_at}.{purpose.value}"
    expected_sig = _sign(payload)
    if not hmac.compare_digest(expected_sig, signature):
        raise IngestTokenError("ingest token signature mismatch")

    current = now if now is not None else int(time.time())
    if expires_at < current:
        raise IngestTokenError("ingest token expired")

    if (
        require_purpose is not None
        and purpose is not IngestPurpose.ANY
        and purpose != require_purpose
    ):
        raise IngestTokenError(
            f"ingest token purpose {purpose.value!r} does not match required "
            f"{require_purpose.value!r}"
        )

    return IngestPrincipal(run_id=run_id, purpose=purpose, expires_at=expires_at)


def require_ingest_token(
    *,
    authorization: str | None,
    run_id: str,
    purpose: IngestPurpose | None = None,
) -> IngestPrincipal:
    """FastAPI helper to verify the ``Authorization: Bearer <ingest_token>`` header."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": 'Bearer realm="caretaker-runs"'},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer scheme",
            headers={"WWW-Authenticate": 'Bearer realm="caretaker-runs"'},
        )
    try:
        return verify(parts[1].strip(), run_id=run_id, require_purpose=purpose)
    except IngestTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc


__all__ = [
    "IngestPrincipal",
    "IngestPurpose",
    "IngestTokenError",
    "issue",
    "require_ingest_token",
    "verify",
]
