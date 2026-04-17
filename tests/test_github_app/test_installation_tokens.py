"""Tests for the installation-token cache and minter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from caretaker.github_app.installation_tokens import (
    InstallationToken,
    InstallationTokenCache,
    InstallationTokenMinter,
    _parse_expiry,
)
from caretaker.github_app.jwt_signer import AppJWTSigner

# ── Cache --------------------------------------------------------------


async def test_cache_round_trip() -> None:
    cache = InstallationTokenCache()
    token = InstallationToken(token="ghs_abc", expires_at=2_000, installation_id=99)

    assert await cache.get(99) is None
    await cache.put(token)
    assert await cache.get(99) == token

    await cache.invalidate(99)
    assert await cache.get(99) is None


async def test_cache_clear_removes_all_tokens() -> None:
    cache = InstallationTokenCache()
    await cache.put(InstallationToken(token="a", expires_at=1, installation_id=1))
    await cache.put(InstallationToken(token="b", expires_at=2, installation_id=2))
    await cache.clear()
    assert await cache.get(1) is None
    assert await cache.get(2) is None


def test_installation_token_is_fresh() -> None:
    token = InstallationToken(token="t", expires_at=1_000, installation_id=1)
    assert token.is_fresh(now=500, skew_seconds=60) is True
    # Within the skew window → stale.
    assert token.is_fresh(now=950, skew_seconds=60) is False
    # After expiry → stale.
    assert token.is_fresh(now=1_100, skew_seconds=60) is False


# ── Expiry parsing -----------------------------------------------------


def test_parse_expiry_z_suffix() -> None:
    # 2026-04-17T00:00:00Z == 1776384000
    # (verified: datetime(2026,4,17,tzinfo=timezone.utc).timestamp())
    assert _parse_expiry("2026-04-17T00:00:00Z") == 1_776_384_000


def test_parse_expiry_offset_suffix() -> None:
    assert _parse_expiry("2026-04-17T00:00:00+00:00") == 1_776_384_000


def test_parse_expiry_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_expiry("not a date")


# ── Minter -------------------------------------------------------------


@respx.mock
async def test_minter_fetches_and_caches(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    route = respx.post("https://api.github.com/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"},
        )
    )

    async with InstallationTokenMinter(signer=signer) as minter:
        t1 = await minter.get_token(42, now=0)
        t2 = await minter.get_token(42, now=60)

    assert t1.token == "ghs_abc"
    assert t1.expires_at > 4_000_000_000
    assert t1 == t2  # served from cache
    assert route.call_count == 1


@respx.mock
async def test_minter_remints_when_token_near_expiry(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    responses = [
        httpx.Response(
            201,
            json={"token": "ghs_first", "expires_at": "1970-01-01T00:16:40Z"},
        ),  # expires_at == 1000
        httpx.Response(
            201,
            json={"token": "ghs_second", "expires_at": "2099-01-01T00:00:00Z"},
        ),
    ]
    respx.post("https://api.github.com/app/installations/42/access_tokens").mock(
        side_effect=responses
    )

    async with InstallationTokenMinter(signer=signer, refresh_skew_seconds=60) as minter:
        # Well before expiry — fresh token from first response.
        first = await minter.get_token(42, now=100)
        assert first.token == "ghs_first"
        # Within the skew window → re-mint using second response.
        second = await minter.get_token(42, now=950)
        assert second.token == "ghs_second"


@respx.mock
async def test_minter_raises_on_http_error(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    respx.post("https://api.github.com/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(401, text="bad creds")
    )

    async with InstallationTokenMinter(signer=signer) as minter:
        with pytest.raises(RuntimeError, match="failed to mint installation token"):
            await minter.get_token(42)


@respx.mock
async def test_minter_raises_on_malformed_response(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    respx.post("https://api.github.com/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t"})
    )

    async with InstallationTokenMinter(signer=signer) as minter:
        with pytest.raises(RuntimeError, match="malformed installation-token"):
            await minter.get_token(42)


async def test_minter_rejects_non_positive_installation(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    minter = InstallationTokenMinter(signer=signer)
    with pytest.raises(ValueError):
        await minter.get_token(0)


async def test_minter_invalidate_forces_refresh(rsa_private_pem: str) -> None:
    signer = AppJWTSigner(app_id=1, private_key_pem=rsa_private_pem)
    minter = InstallationTokenMinter(signer=signer)

    async def fake_mint(*, installation_id: int) -> InstallationToken:
        return InstallationToken(
            token=f"tok-{fake_mint.calls}",  # type: ignore[attr-defined]
            expires_at=9_999_999_999,
            installation_id=installation_id,
        )

    fake_mint.calls = 0  # type: ignore[attr-defined]

    async def counting_mint(*, installation_id: int) -> InstallationToken:
        fake_mint.calls += 1  # type: ignore[attr-defined]
        return await fake_mint(installation_id=installation_id)

    minter._mint = AsyncMock(side_effect=counting_mint)  # type: ignore[method-assign]

    t1 = await minter.get_token(7, now=0)
    t2 = await minter.get_token(7, now=1)
    assert t1 == t2
    assert fake_mint.calls == 1  # type: ignore[attr-defined]

    await minter.invalidate(7)
    await minter.get_token(7, now=2)
    assert fake_mint.calls == 2  # type: ignore[attr-defined]
