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


def _comment_payload(login: str = "maintainer") -> dict[str, object]:
    return {
        "id": 123,
        "user": {"login": login, "id": 1},
        "body": "comment body",
        "created_at": "2026-04-14T12:00:00Z",
        "updated_at": "2026-04-14T12:00:00Z",
    }


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
    assert "No retry time specified" in str(error)


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


@pytest.mark.asyncio
async def test_add_issue_comment_routes_copilot_mentions_through_copilot_client() -> None:
    default_client = AsyncMock()
    default_client.request = AsyncMock(return_value=_make_response(201, _comment_payload("bot")))
    copilot_client = AsyncMock()
    copilot_client.request = AsyncMock(return_value=_make_response(201, _comment_payload("me")))

    with patch.object(
        GitHubClient,
        "_build_client",
        side_effect=[default_client, copilot_client],
    ):
        gh = GitHubClient(token="default-token", copilot_token="user-pat")

    comment = await gh.add_issue_comment("o", "r", 7, "@copilot please fix this")

    copilot_client.request.assert_awaited_once()
    default_client.request.assert_not_awaited()
    assert comment.user.login == "me"


@pytest.mark.asyncio
async def test_add_issue_comment_uses_default_client_for_regular_comments() -> None:
    default_client = AsyncMock()
    default_client.request = AsyncMock(
        return_value=_make_response(201, _comment_payload("github-actions[bot]"))
    )
    copilot_client = AsyncMock()
    copilot_client.request = AsyncMock(return_value=_make_response(201, _comment_payload("me")))

    with patch.object(
        GitHubClient,
        "_build_client",
        side_effect=[default_client, copilot_client],
    ):
        gh = GitHubClient(token="default-token", copilot_token="user-pat")

    comment = await gh.add_issue_comment("o", "r", 7, "Regular maintainer note")

    default_client.request.assert_awaited_once()
    copilot_client.request.assert_not_awaited()
    assert comment.user.login == "github-actions[bot]"


# ── In-process read cache ─────────────────────────────────────────────────────


def _repo_payload() -> dict[str, object]:
    return {
        "full_name": "o/r",
        "name": "r",
        "owner": {"login": "o"},
        "default_branch": "main",
        "private": False,
    }


def _pr_payload(number: int, title: str, state: str, merged: bool) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "user": {"login": "a", "id": 1},
        "head": {"ref": "feat"},
        "base": {"ref": "main"},
        "labels": [],
        "merged": merged,
        "draft": False,
        "html_url": "http://x",
        "mergeable": None,
        "merged_at": None,
    }


@pytest.mark.asyncio
async def test_read_cache_returns_cached_response_on_second_call() -> None:
    """A second identical GET should use the cached response, not call the network."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(200, _repo_payload()))

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    # First call — goes to the network
    await gh._get("/repos/o/r")
    # Second call — should hit the cache
    await gh._get("/repos/o/r")

    # Network should only have been called once
    assert mock_client.request.await_count == 1


@pytest.mark.asyncio
async def test_read_cache_distinguishes_different_params() -> None:
    """GET calls with different params must produce separate cache entries."""
    open_payload = [_pr_payload(1, "open pr", "open", False)]
    closed_payload = [_pr_payload(2, "closed pr", "closed", True)]

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        side_effect=[
            _make_response(200, open_payload),
            _make_response(200, closed_payload),
        ]
    )

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    result_open = await gh.list_pull_requests("o", "r", state="open")
    result_closed = await gh.list_pull_requests("o", "r", state="closed")

    assert len(result_open) == 1
    assert len(result_closed) == 1
    assert result_open[0].number == 1
    assert result_closed[0].number == 2
    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_read_cache_clear_invalidates_entries() -> None:
    """clear_read_cache must cause the next GET to go to the network again."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(200, _repo_payload()))

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    await gh._get("/repos/o/r")
    gh.clear_read_cache()
    await gh._get("/repos/o/r")

    # Cache was cleared, so two network calls are expected
    assert mock_client.request.await_count == 2


@pytest.mark.asyncio
async def test_read_cache_does_not_cache_none_responses() -> None:
    """404 (None) responses must not be stored in the cache — repeated GETs must
    hit the network again so a resource created between calls is eventually found."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        side_effect=[
            _make_response(404),  # first call: not found
            _make_response(200, _repo_payload()),  # second call: now exists
        ]
    )

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    first = await gh._get("/repos/o/r")
    second = await gh._get("/repos/o/r")

    assert first is None
    assert second is not None
    # Both calls went to the network (None was not cached)
    assert mock_client.request.await_count == 2


# ── approve_workflow_run ──────────────────────────────────────────────────────


def _make_httpx_response(
    status_code: int,
    json_body: dict | None = None,
    text: str = "",
    has_content: bool = True,
) -> MagicMock:
    """Build a mock that resembles an httpx Response (not routed through _request_with_client)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = b"body" if has_content else b""
    resp.is_success = 200 <= status_code < 300
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


@pytest.mark.asyncio
async def test_approve_workflow_run_204_returns_true() -> None:
    """A 204 response (success, no content) returns True."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_httpx_response(204, has_content=False))

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    result = await gh.approve_workflow_run("o", "r", 42)
    assert result is True
    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_workflow_run_404_returns_false() -> None:
    """A 404 response returns False (workflow run not found)."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=_make_httpx_response(404, text="Not Found"))

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    result = await gh.approve_workflow_run("o", "r", 999)
    assert result is False


@pytest.mark.asyncio
async def test_approve_workflow_run_error_raises_githubapieerror() -> None:
    """A non-success, non-404 response raises GitHubAPIError."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=_make_httpx_response(422, text="Unprocessable Entity")
    )

    with patch.object(GitHubClient, "_build_client", return_value=mock_client):
        gh = GitHubClient(token="tok")

    with pytest.raises(GitHubAPIError) as exc_info:
        await gh.approve_workflow_run("o", "r", 55)

    assert exc_info.value.status_code == 422
