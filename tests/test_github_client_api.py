"""Tests for GitHubClient._request_with_client — specifically 403 rate-limit handling."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def _make_mock_client(
    status_code: int, body: dict | str, extra_headers: dict | None = None
) -> MagicMock:
    """Return a mock httpx.AsyncClient whose .request() coroutine returns the given response."""
    if isinstance(body, dict):
        content = json.dumps(body).encode()
        headers = {"content-type": "application/json"}
    else:
        content = body.encode()
        headers = {"content-type": "text/plain"}
    if extra_headers:
        headers.update(extra_headers)
    resp = httpx.Response(status_code=status_code, content=content, headers=headers)
    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=resp)
    return mock_client


@pytest.fixture()
def api_client() -> GitHubClient:
    with patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}):
        return GitHubClient(token="tok")


class TestRateLimitDetection:
    """403 responses containing 'rate limit' should be raised as 429."""

    @pytest.mark.asyncio
    async def test_403_rate_limit_body_raised_as_429(self, api_client: GitHubClient) -> None:
        body = {
            "message": "API rate limit exceeded for installation.",
            "documentation_url": "https://docs.github.com/en/rest/using-the-rest-api/getting-started-with-the-rest-api#rate-limiting",
            "status": "403",
        }
        mock_client = _make_mock_client(403, body)

        with pytest.raises(GitHubAPIError) as exc_info:
            await api_client._request_with_client(mock_client, "GET", "/test")

        assert exc_info.value.status_code == 429
        assert "Rate limited" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_403_rate_limit_with_retry_after_header(self, api_client: GitHubClient) -> None:
        body = {"message": "API rate limit exceeded for installation.", "status": "403"}
        mock_client = _make_mock_client(403, body, extra_headers={"Retry-After": "30"})

        with pytest.raises(GitHubAPIError) as exc_info:
            await api_client._request_with_client(mock_client, "GET", "/test")

        assert exc_info.value.status_code == 429
        assert "30" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_403_forbidden_non_rate_limit_stays_403(self, api_client: GitHubClient) -> None:
        """A normal permission-denied 403 must NOT be converted to 429."""
        body = {"message": "Resource not accessible by integration", "status": "403"}
        mock_client = _make_mock_client(403, body)

        with pytest.raises(GitHubAPIError) as exc_info:
            await api_client._request_with_client(mock_client, "GET", "/test")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_403_plain_text_non_rate_limit_stays_403(self, api_client: GitHubClient) -> None:
        """Plain-text 403 with no rate-limit content stays 403."""
        mock_client = _make_mock_client(403, "Forbidden")

        with pytest.raises(GitHubAPIError) as exc_info:
            await api_client._request_with_client(mock_client, "GET", "/test")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_429_still_handled_as_before(self, api_client: GitHubClient) -> None:
        mock_client = _make_mock_client(429, {"message": "Too many requests"})

        with pytest.raises(GitHubAPIError) as exc_info:
            await api_client._request_with_client(mock_client, "GET", "/test")

        assert exc_info.value.status_code == 429
