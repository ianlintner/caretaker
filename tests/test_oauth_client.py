"""Tests for :mod:`caretaker.auth.oauth_client`."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from caretaker.auth import (
    OAuth2ClientCredentials,
    OAuth2TokenError,
    build_client_from_env,
)


class _MockTransport(httpx.MockTransport):
    """Mock transport that counts requests and serves scripted responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._iter: Iterator[httpx.Response] = iter(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise AssertionError("mock transport ran out of responses") from exc


def _success_response(
    access_token: str = "tok-xyz", expires_in: int = 3600
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": expires_in,
        },
    )


@pytest.mark.asyncio
async def test_get_token_fetches_once_and_caches() -> None:
    transport = _MockTransport([_success_response("abc")])
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
            scope="read write",
        )
        first = await oauth.get_token(client=client)
        second = await oauth.get_token(client=client)

    assert first == "abc"
    assert second == "abc"
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.method == "POST"
    assert b"grant_type=client_credentials" in req.content
    assert b"scope=read+write" in req.content
    # client_secret_basic auth — Basic <base64(id:secret)>
    assert req.headers["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_scope_omitted_when_empty() -> None:
    transport = _MockTransport([_success_response()])
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        await oauth.get_token(client=client)

    assert b"scope=" not in transport.requests[0].content


@pytest.mark.asyncio
async def test_refresh_when_token_expires() -> None:
    # expires_in=60 is clamped to the minimum 60s; force expiry by invalidating.
    transport = _MockTransport(
        [
            _success_response("first", expires_in=60),
            _success_response("second", expires_in=3600),
        ]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        assert await oauth.get_token(client=client) == "first"
        oauth.invalidate()
        assert await oauth.get_token(client=client) == "second"

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_concurrent_callers_coalesce_on_single_refresh() -> None:
    transport = _MockTransport([_success_response("once")])
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        results = await asyncio.gather(
            *(oauth.get_token(client=client) for _ in range(10))
        )

    assert results == ["once"] * 10
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_non_200_raises_token_error() -> None:
    transport = _MockTransport(
        [httpx.Response(401, json={"error": "invalid_client"})]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        with pytest.raises(OAuth2TokenError, match="401"):
            await oauth.get_token(client=client)


@pytest.mark.asyncio
async def test_missing_access_token_field_raises() -> None:
    transport = _MockTransport([httpx.Response(200, json={"token_type": "Bearer"})])
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        with pytest.raises(OAuth2TokenError, match="access_token"):
            await oauth.get_token(client=client)


@pytest.mark.asyncio
async def test_transport_error_raises_token_error() -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_raise)) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        with pytest.raises(OAuth2TokenError, match="transport error"):
            await oauth.get_token(client=client)


@pytest.mark.asyncio
async def test_authorization_header_shape() -> None:
    transport = _MockTransport([_success_response("zzz")])
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        headers = await oauth.authorization_header(client=client)
    assert headers == {"Authorization": "Bearer zzz"}


def test_constructor_rejects_empty_fields() -> None:
    with pytest.raises(ValueError):
        OAuth2ClientCredentials(
            client_id="",
            client_secret="s",
            token_url="https://x",
        )


def test_build_client_from_env_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("OAUTH2_CLIENT_ID", "OAUTH2_CLIENT_SECRET", "OAUTH2_TOKEN_URL"):
        monkeypatch.delenv(var, raising=False)
    assert build_client_from_env() is None


def test_build_client_from_env_succeeds_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OAUTH2_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "csec")
    monkeypatch.setenv("OAUTH2_TOKEN_URL", "https://auth.test/oauth/token")
    monkeypatch.setenv("OAUTH2_SCOPE", "read")
    client = build_client_from_env()
    assert client is not None


@pytest.mark.asyncio
async def test_expires_in_clamped_to_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _MockTransport(
        [httpx.Response(200, json={"access_token": "t", "expires_in": 0})]
    )
    async with httpx.AsyncClient(transport=transport) as client:
        oauth = OAuth2ClientCredentials(
            client_id="id",
            client_secret="secret",
            token_url="https://auth.test/oauth/token",
        )
        await oauth.get_token(client=client)

    # After the fetch, is_valid() at t+30s skew boundary should still hold
    # because expires_in=0 was clamped up to 60s.
    cached: Any = oauth._cached  # noqa: SLF001 — test-only access
    assert cached is not None
    assert cached.expires_at_monotonic > time.monotonic() + 20
