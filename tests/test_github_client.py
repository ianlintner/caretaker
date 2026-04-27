"""Tests for GitHubClient._request_with_client error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient, RateLimitError


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


@pytest.fixture
def github_client() -> GitHubClient:
    """A GitHubClient instance with a minimal test token."""
    return GitHubClient(token="test-token")


class TestRequestWithClientErrorHandling:
    @pytest.mark.asyncio
    async def test_403_rate_limit_raises_rate_limit_error(
        self, github_client: GitHubClient
    ) -> None:
        """A 403 with 'rate limit' in the message raises RateLimitError."""
        rate_limit_body = {
            "message": "API rate limit exceeded for installation.",
            "status": "403",
        }
        resp = make_response(403, json_body=rate_limit_body)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with (
            patch("caretaker.github_client.api.get_cooldown") as mock_cooldown,
            patch("caretaker.github_client.api.record_response_headers"),
            patch(
                "caretaker.github_client.api.record_rate_limit_response",
                return_value=0.0,
            ),
        ):
            mock_cooldown.return_value.is_blocked.return_value = False
            with pytest.raises(RateLimitError) as exc_info:
                await github_client._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403
        assert "Rate limited" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_403_auth_error_raises_github_api_error(
        self, github_client: GitHubClient
    ) -> None:
        """A 403 auth error (not rate limit) still raises GitHubAPIError."""
        auth_body = {"message": "Forbidden", "status": "403"}
        resp = make_response(403, json_body=auth_body)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with (
            patch("caretaker.github_client.api.get_cooldown") as mock_cooldown,
            patch("caretaker.github_client.api.record_response_headers"),
            patch("caretaker.github_client.api.is_scope_gap_message", return_value=False),
        ):
            mock_cooldown.return_value.is_blocked.return_value = False
            with pytest.raises(GitHubAPIError) as exc_info:
                await github_client._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit_error_with_retry_info(
        self, github_client: GitHubClient
    ) -> None:
        """A 429 response raises RateLimitError with retry info."""
        resp = make_response(429)
        resp.headers = {"Retry-After": "30"}
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with (
            patch("caretaker.github_client.api.get_cooldown") as mock_cooldown,
            patch("caretaker.github_client.api.record_response_headers"),
            patch(
                "caretaker.github_client.api.record_rate_limit_response",
                return_value=time_plus(30),
            ),
        ):
            mock_cooldown.return_value.is_blocked.return_value = False
            with pytest.raises(RateLimitError) as exc_info:
                await github_client._request_with_client(mock_client, "GET", "/repos/o/r/issues")

        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_404_returns_none(self, github_client: GitHubClient) -> None:
        """A 404 response returns None."""
        resp = make_response(404)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with (
            patch("caretaker.github_client.api.get_cooldown") as mock_cooldown,
            patch("caretaker.github_client.api.record_response_headers"),
        ):
            mock_cooldown.return_value.is_blocked.return_value = False
            result = await github_client._request_with_client(
                mock_client, "GET", "/repos/o/r/issues/999"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_204_returns_none(self, github_client: GitHubClient) -> None:
        """A 204 No Content response returns None."""
        resp = make_response(204)
        mock_client = AsyncMock()
        mock_client.request.return_value = resp

        with (
            patch("caretaker.github_client.api.get_cooldown") as mock_cooldown,
            patch("caretaker.github_client.api.record_response_headers"),
        ):
            mock_cooldown.return_value.is_blocked.return_value = False
            result = await github_client._request_with_client(
                mock_client, "DELETE", "/repos/o/r/issues/1/labels/x"
            )

        assert result is None


def time_plus(seconds: float) -> float:
    import time

    return time.time() + seconds
