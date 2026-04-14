"""Tests for GitHubClient._request_with_client error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def make_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    if json_body is not None:
        resp.json.return_value = json_body
        resp.text = str(json_body)
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


class TestRequestWithClientErrorHandling:
    @pytest.mark.asyncio
    async def test_403_rate_limit_raises_github_api_error(self) -> None:
        """A 403 response with 'rate limit' in the message raises GitHubAPIError."""
        client = MagicMock(spec=GitHubClient)
        rate_limit_body = {
            "message": "API rate limit exceeded for installation.",
            "status": "403",
        }
        resp = make_response(403, json_body=rate_limit_body)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        gh = GitHubClient.__new__(GitHubClient)

        with pytest.raises(GitHubAPIError) as exc_info:
            await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403
        assert "Rate limited" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_403_auth_error_raises_github_api_error(self) -> None:
        """A 403 auth error (not rate limit) still raises GitHubAPIError."""
        gh = GitHubClient.__new__(GitHubClient)
        auth_body = {"message": "Forbidden", "status": "403"}
        resp = make_response(403, json_body=auth_body)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_429_raises_github_api_error_with_retry_info(self) -> None:
        """A 429 response raises GitHubAPIError with retry info."""
        gh = GitHubClient.__new__(GitHubClient)
        resp = make_response(429)
        resp.headers = {"Retry-After": "30"}
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with pytest.raises(GitHubAPIError) as exc_info:
            await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 429
        assert "30" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_404_returns_none(self) -> None:
        """A 404 response returns None."""
        gh = GitHubClient.__new__(GitHubClient)
        resp = make_response(404)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        result = await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues/999")

        assert result is None

    @pytest.mark.asyncio
    async def test_204_returns_none(self) -> None:
        """A 204 No Content response returns None."""
        gh = GitHubClient.__new__(GitHubClient)
        resp = make_response(204)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        result = await gh._request_with_client(
            mock_client, "DELETE", "/repos/o/r/issues/1/labels/x"
        )

        assert result is None
