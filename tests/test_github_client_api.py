"""Tests for GitHubClient._request_with_client error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def _make_response(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    if json_body is not None:
        resp.json.return_value = json_body
        resp.text = str(json_body)
    else:
        resp.json.side_effect = Exception("no json")
    return resp


@pytest.mark.asyncio
async def test_403_rate_limit_raises_githubapieerror() -> None:
    """A 403 response with 'rate limit exceeded' message is treated as rate-limiting."""
    # We test _request_with_client directly by instantiating with a fake token
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    rate_limit_body = {
        "message": "API rate limit exceeded for installation.",
        "documentation_url": "https://docs.github.com/en/rest",
        "status": "403",
    }
    resp = _make_response(403, json_body=rate_limit_body)

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError) as exc_info:
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

    error = exc_info.value
    assert error.status_code == 403
    assert "Rate limited" in str(error)


@pytest.mark.asyncio
async def test_403_non_rate_limit_raises_githubapieerror() -> None:
    """A regular 403 (not rate limit) raises GitHubAPIError with the response body."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    forbidden_body = {"message": "Must have admin rights to Repository.", "status": "403"}
    resp = _make_response(403, json_body=forbidden_body)

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError) as exc_info:
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/settings")

    error = exc_info.value
    assert error.status_code == 403
    assert "Rate limited" not in str(error)


@pytest.mark.asyncio
async def test_403_rate_limit_with_retry_after_header() -> None:
    """A 403 rate-limit with Retry-After header includes the retry delay in the message."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    rate_limit_body = {
        "message": "API rate limit exceeded for installation.",
        "status": "403",
    }
    resp = _make_response(403, json_body=rate_limit_body)
    resp.headers = {"Retry-After": "30"}

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError) as exc_info:
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

    error = exc_info.value
    assert error.status_code == 403
    assert "Rate limited" in str(error)
    assert "30" in str(error)


@pytest.mark.asyncio
async def test_429_raises_githubapieerror_with_retry_after() -> None:
    """A 429 response raises GitHubAPIError with Retry-After info."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    resp = _make_response(429, text="Too Many Requests")
    resp.headers = {"Retry-After": "60"}

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp)

    with pytest.raises(GitHubAPIError) as exc_info:
        await gh._request_with_client(mock_client, "GET", "/repos/o/r/issues")

    error = exc_info.value
    assert error.status_code == 429
    assert "Rate limited" in str(error)
    assert "60" in str(error)
