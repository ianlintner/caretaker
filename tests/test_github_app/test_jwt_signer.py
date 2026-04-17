"""Tests for ``AppJWTSigner``.

These tests generate a fresh RSA key at session scope (see
``conftest.py``) so we exercise the real PyJWT RS256 code path without
shipping a private key in the repo.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest

from caretaker.github_app.jwt_signer import AppJWTSigner


def test_issue_produces_valid_rs256_jwt(rsa_private_pem: str, rsa_public_pem: str) -> None:
    signer = AppJWTSigner(app_id=12345, private_key_pem=rsa_private_pem)
    token = signer.issue(now=1_000_000)

    decoded = pyjwt.decode(
        token,
        rsa_public_pem,
        algorithms=["RS256"],
        # verify_exp=False because we issue with a synthetic past `now`; the
        # signature and claim values are what we are testing here.
        options={"verify_signature": True, "verify_exp": False},
    )
    assert decoded["iss"] == "12345"
    # iat is clock-skewed backwards by 60s; exp is 9 min ahead.
    assert decoded["iat"] == 1_000_000 - 60
    assert decoded["exp"] == 1_000_000 + 9 * 60


def test_issue_caches_token_while_fresh(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=42, private_key_pem=rsa_private_pem)
    t1 = signer.issue(now=1_000)
    t2 = signer.issue(now=1_000 + 60)  # Well within the 9-min window
    assert t1 == t2


def test_issue_refreshes_when_cache_is_stale(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=42, private_key_pem=rsa_private_pem)
    t1 = signer.issue(now=1_000)
    # Beyond expiry minus the 60s skew → must re-mint.
    t2 = signer.issue(now=1_000 + 9 * 60)
    assert t1 != t2


def test_invalid_app_id_rejected(rsa_private_pem: str) -> None:
    with pytest.raises(ValueError):
        AppJWTSigner(app_id=0, private_key_pem=rsa_private_pem)
    with pytest.raises(ValueError):
        AppJWTSigner(app_id=-7, private_key_pem=rsa_private_pem)


def test_empty_private_key_rejected() -> None:
    with pytest.raises(ValueError):
        AppJWTSigner(app_id=1, private_key_pem="   \n")
