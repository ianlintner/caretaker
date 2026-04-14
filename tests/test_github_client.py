"""Tests for the GitHub REST API client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def _make_response(
    status_code: int,
    body: dict | str | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if isinstance(body, dict):
        resp.text = json.dumps(body)
        resp.json.return_value = body
    else:
        resp.text = body or ""
        resp.json.side_effect = ValueError("not JSON")
    return resp


@pytest.fixture()
def client() -> GitHubClient:
    return GitHubClient(token="test-token")


class TestRequestWithClient:
    async def test_404_returns_none(self, client: GitHubClient) -> None:
        resp = _make_response(404)
        http_client = AsyncMock()
        http_client.request.return_value = resp

        result = await client._request_with_client(http_client, "GET", "/repos/o/r")

        assert result is None

    async def test_204_returns_none(self, client: GitHubClient) -> None:
        resp = _make_response(204)
        http_client = AsyncMock()
        http_client.request.return_value = resp

        result = await client._request_with_client(http_client, "DELETE", "/repos/o/r/labels/1")

        assert result is None

    async def test_429_raises_rate_limit_error(self, client: GitHubClient) -> None:
        resp = _make_response(429, headers={"Retry-After": "30"})
        http_client = AsyncMock()
        http_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await client._request_with_client(http_client, "GET", "/repos/o/r")

        assert exc_info.value.status_code == 429
        assert "Rate limited" in str(exc_info.value)
        assert "30" in str(exc_info.value)

    async def test_403_rate_limit_message_raises_rate_limited_error(
        self, client: GitHubClient
    ) -> None:
        """GitHub returns 403 (not 429) for primary/secondary rate limit exhaustion."""
        body = {
            "message": "API rate limit exceeded for installation.",
            "documentation_url": "https://docs.github.com/en/rest/rate-limiting",
            "status": "403",
        }
        resp = _make_response(403, body=body, headers={"Retry-After": "45"})
        http_client = AsyncMock()
        http_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await client._request_with_client(http_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403
        assert "Rate limited" in str(exc_info.value)
        assert "45" in str(exc_info.value)

    async def test_403_non_rate_limit_raises_generic_api_error(self, client: GitHubClient) -> None:
        """403 responses that are not rate-limit errors should raise a plain GitHubAPIError."""
        resp = _make_response(403, body={"message": "Resource not accessible by integration"})
        http_client = AsyncMock()
        http_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await client._request_with_client(http_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403
        # Should NOT be the rate-limit variant
        assert "Rate limited" not in str(exc_info.value)

    async def test_500_raises_api_error(self, client: GitHubClient) -> None:
        resp = _make_response(500, body="Internal Server Error")
        http_client = AsyncMock()
        http_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await client._request_with_client(http_client, "GET", "/repos/o/r")

        assert exc_info.value.status_code == 500

    async def test_200_returns_json_body(self, client: GitHubClient) -> None:
        body = {"id": 1, "number": 42, "title": "Test issue"}
        resp = _make_response(200, body=body)
        http_client = AsyncMock()
        http_client.request.return_value = resp

        result = await client._request_with_client(http_client, "GET", "/repos/o/r/issues/42")

        assert result == body
