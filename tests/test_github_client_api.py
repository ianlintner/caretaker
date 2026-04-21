"""Tests for GitHubClient._request_with_client error handling."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient
from caretaker.github_client.credentials import StaticCredentialsProvider

_TS = _dt(2026, 4, 19, tzinfo=UTC)


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
    # RateLimitError subclass carries a retry_after_seconds; when the
    # server omits both Retry-After and X-RateLimit-Reset, the client
    # falls back to a 60s cushion rather than propagating an
    # unbounded "no retry time specified" sentinel.
    from caretaker.github_client.api import RateLimitError

    assert isinstance(error, RateLimitError)
    assert error.retry_after_seconds is not None
    assert error.retry_after_seconds > 0


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
    mock_creds = AsyncMock(spec=StaticCredentialsProvider)
    mock_creds.default_token = AsyncMock(return_value="default-token")
    mock_creds.copilot_token = AsyncMock(return_value="user-pat")

    gh = GitHubClient(credentials_provider=mock_creds)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(201, _comment_payload("me")))
    gh._client = mock_client

    comment = await gh.add_issue_comment("o", "r", 7, "@copilot please fix this")

    mock_creds.copilot_token.assert_awaited_once()
    mock_creds.default_token.assert_not_awaited()
    assert comment.user.login == "me"


@pytest.mark.asyncio
async def test_add_issue_comment_uses_default_client_for_regular_comments() -> None:
    mock_creds = AsyncMock(spec=StaticCredentialsProvider)
    mock_creds.default_token = AsyncMock(return_value="default-token")
    mock_creds.copilot_token = AsyncMock(return_value="user-pat")

    gh = GitHubClient(credentials_provider=mock_creds)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value=_make_response(201, _comment_payload("github-actions[bot]"))
    )
    gh._client = mock_client

    comment = await gh.add_issue_comment("o", "r", 7, "Regular maintainer note")

    mock_creds.default_token.assert_awaited_once()
    mock_creds.copilot_token.assert_not_awaited()
    assert comment.user.login == "github-actions[bot]"


@pytest.mark.asyncio
async def test_edit_issue_comment_sends_patch() -> None:
    """edit_issue_comment issues PATCH to /issues/comments/:id and returns the Comment."""
    with patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}):
        gh = GitHubClient(token="fake-token")

    payload = _comment_payload("github-actions[bot]")
    payload["body"] = "new body"
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(200, payload))
    gh._client = mock_client

    comment = await gh.edit_issue_comment("o", "r", 123, "new body")

    assert comment.id == 123
    assert comment.body == "new body"
    call = mock_client.request.await_args
    assert call.args[0] == "PATCH"
    assert call.args[1].endswith("/repos/o/r/issues/comments/123")
    assert call.kwargs["json"] == {"body": "new body"}


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


def test_parse_pr_populates_head_and_base_repo_full_name() -> None:
    """_parse_pr reads repo.full_name so the fork check can work."""
    data = {
        "number": 7,
        "title": "fork pr",
        "body": "",
        "state": "open",
        "user": {"login": "contributor", "id": 1},
        "head": {
            "ref": "feat",
            "sha": "abc",
            "repo": {"full_name": "contributor/caretaker"},
        },
        "base": {
            "ref": "main",
            "sha": "def",
            "repo": {"full_name": "ianlintner/caretaker"},
        },
    }
    pr = GitHubClient._parse_pr(data)
    assert pr.head_repo_full_name == "contributor/caretaker"
    assert pr.base_repo_full_name == "ianlintner/caretaker"
    assert pr.is_fork is True


def test_parse_pr_is_fork_false_for_same_repo() -> None:
    data = {
        "number": 8,
        "title": "internal",
        "body": "",
        "state": "open",
        "user": {"login": "bot", "id": 2},
        "head": {"ref": "b", "sha": "a", "repo": {"full_name": "ianlintner/caretaker"}},
        "base": {"ref": "main", "sha": "d", "repo": {"full_name": "ianlintner/caretaker"}},
    }
    pr = GitHubClient._parse_pr(data)
    assert pr.is_fork is False


def test_parse_pr_handles_missing_repo_block() -> None:
    """A PR without repo metadata must not crash; is_fork stays False."""
    data = {
        "number": 9,
        "title": "minimal",
        "body": "",
        "state": "open",
        "user": {"login": "bot", "id": 2},
        "head": {"ref": "b", "sha": "a"},
        "base": {"ref": "main", "sha": "d"},
    }
    pr = GitHubClient._parse_pr(data)
    assert pr.head_repo_full_name == ""
    assert pr.base_repo_full_name == ""
    assert pr.is_fork is False


# ── upsert_issue_comment ─────────────────────────────────────────────────────


def _client_with_token() -> GitHubClient:
    return GitHubClient(credentials_provider=StaticCredentialsProvider(default_token="x"))


@pytest.mark.asyncio
async def test_upsert_issue_comment_posts_when_no_existing() -> None:
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test-marker -->"
    body = f"{marker}\nhello"

    posted = Comment(
        id=1,
        body=body,
        user=User(login="bot", id=0, type="Bot"),
        created_at=_TS,
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[])),
        patch.object(client, "add_issue_comment", AsyncMock(return_value=posted)) as add_mock,
        patch.object(client, "edit_issue_comment", AsyncMock()) as edit_mock,
    ):
        result = await client.upsert_issue_comment("o", "r", 1, marker, body)

    add_mock.assert_awaited_once_with("o", "r", 1, body)
    edit_mock.assert_not_awaited()
    assert result.id == 1


@pytest.mark.asyncio
async def test_upsert_issue_comment_edits_when_body_differs() -> None:
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test-marker -->"
    new_body = f"{marker}\nnew"

    existing = Comment(
        id=42,
        body=f"{marker}\nold",
        user=User(login="bot", id=0, type="Bot"),
        created_at=_TS,
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[existing])),
        patch.object(client, "add_issue_comment", AsyncMock()) as add_mock,
        patch.object(client, "edit_issue_comment", AsyncMock(return_value=existing)) as edit_mock,
    ):
        await client.upsert_issue_comment("o", "r", 1, marker, new_body)

    add_mock.assert_not_awaited()
    edit_mock.assert_awaited_once_with("o", "r", 42, new_body)


@pytest.mark.asyncio
async def test_upsert_issue_comment_noop_when_body_unchanged() -> None:
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test-marker -->"
    body = f"{marker}\nsame"

    existing = Comment(id=7, body=body, user=User(login="bot", id=0, type="Bot"), created_at=_TS)
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[existing])),
        patch.object(client, "add_issue_comment", AsyncMock()) as add_mock,
        patch.object(client, "edit_issue_comment", AsyncMock()) as edit_mock,
    ):
        result = await client.upsert_issue_comment("o", "r", 1, marker, body)

    add_mock.assert_not_awaited()
    edit_mock.assert_not_awaited()
    assert result.id == 7


@pytest.mark.asyncio
async def test_upsert_issue_comment_picks_newest_match() -> None:
    """If multiple comments carry the marker, the highest-id one is edited."""
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test-marker -->"
    new_body = f"{marker}\nnew"

    user = User(login="bot", id=0, type="Bot")
    older = Comment(id=10, body=f"{marker}\nA", user=user, created_at=_TS)
    newer = Comment(id=20, body=f"{marker}\nB", user=user, created_at=_TS)
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[older, newer])),
        patch.object(client, "add_issue_comment", AsyncMock()),
        patch.object(client, "edit_issue_comment", AsyncMock(return_value=newer)) as edit_mock,
    ):
        await client.upsert_issue_comment("o", "r", 1, marker, new_body)

    edit_mock.assert_awaited_once()
    assert edit_mock.await_args.args[2] == 20


@pytest.mark.asyncio
async def test_upsert_issue_comment_falls_back_to_legacy_marker() -> None:
    """A comment with only the legacy marker is recognized and edited in place."""
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    new_marker = "<!-- caretaker:test-v2 -->"
    legacy_marker = "<!-- caretaker:test-v1 -->"
    new_body = f"{new_marker}\nmigrated"

    legacy = Comment(
        id=99,
        body=f"{legacy_marker}\noldformat",
        user=User(login="bot", id=0, type="Bot"),
        created_at=_TS,
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[legacy])),
        patch.object(client, "add_issue_comment", AsyncMock()) as add_mock,
        patch.object(client, "edit_issue_comment", AsyncMock(return_value=legacy)) as edit_mock,
    ):
        await client.upsert_issue_comment(
            "o", "r", 1, new_marker, new_body, legacy_markers=(legacy_marker,)
        )

    add_mock.assert_not_awaited()
    edit_mock.assert_awaited_once_with("o", "r", 99, new_body)


@pytest.mark.asyncio
async def test_upsert_issue_comment_rejects_body_without_marker() -> None:
    client = _client_with_token()
    with pytest.raises(ValueError, match="missing marker"):
        await client.upsert_issue_comment("o", "r", 1, "<!-- caretaker:x -->", "no marker here")


@pytest.mark.asyncio
async def test_upsert_cooldown_skips_recent_update() -> None:
    """An existing comment updated within the cooldown window is NOT re-edited."""
    from datetime import UTC
    from datetime import datetime as _dt

    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test -->"
    new_body = f"{marker}\nnew"
    fresh = Comment(
        id=42,
        body=f"{marker}\nold",
        user=User(login="bot", id=0, type="Bot"),
        created_at=_dt(2026, 1, 1, tzinfo=UTC),  # ancient
        updated_at=_dt.now(UTC),  # just now
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[fresh])),
        patch.object(client, "edit_issue_comment", AsyncMock()) as edit_mock,
    ):
        result = await client.upsert_issue_comment(
            "o",
            "r",
            1,
            marker,
            new_body,
            min_seconds_between_updates=3600,
        )

    edit_mock.assert_not_awaited()
    assert result.id == 42  # returned the existing untouched


@pytest.mark.asyncio
async def test_upsert_cooldown_allows_update_past_window() -> None:
    """Beyond the cooldown window, the update proceeds normally."""
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test -->"
    new_body = f"{marker}\nnew"
    old = Comment(
        id=42,
        body=f"{marker}\nold",
        user=User(login="bot", id=0, type="Bot"),
        created_at=_dt.now(UTC) - timedelta(hours=2),
        updated_at=_dt.now(UTC) - timedelta(hours=2),  # outside 1h cooldown
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[old])),
        patch.object(client, "edit_issue_comment", AsyncMock(return_value=old)) as edit_mock,
    ):
        await client.upsert_issue_comment(
            "o",
            "r",
            1,
            marker,
            new_body,
            min_seconds_between_updates=3600,
        )

    edit_mock.assert_awaited_once_with("o", "r", 42, new_body)


@pytest.mark.asyncio
async def test_upsert_cooldown_zero_means_always_update() -> None:
    """Default cooldown of 0 must not block any update."""
    from datetime import UTC
    from datetime import datetime as _dt

    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test -->"
    new_body = f"{marker}\nnew"
    fresh = Comment(
        id=42,
        body=f"{marker}\nold",
        user=User(login="bot", id=0, type="Bot"),
        created_at=_dt.now(UTC),
        updated_at=_dt.now(UTC),
    )
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[fresh])),
        patch.object(client, "edit_issue_comment", AsyncMock(return_value=fresh)) as edit_mock,
    ):
        await client.upsert_issue_comment("o", "r", 1, marker, new_body)
    edit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_cooldown_does_not_block_initial_post() -> None:
    """Cooldown only applies to updates of existing comments, not first posts."""
    from caretaker.github_client.models import Comment, User

    client = _client_with_token()
    marker = "<!-- caretaker:test -->"
    body = f"{marker}\nfirst"
    posted = Comment(id=1, body=body, user=User(login="b", id=0, type="Bot"), created_at=_TS)
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=[])),
        patch.object(client, "add_issue_comment", AsyncMock(return_value=posted)) as add_mock,
    ):
        result = await client.upsert_issue_comment(
            "o",
            "r",
            1,
            marker,
            body,
            min_seconds_between_updates=3600,
        )
    add_mock.assert_awaited_once()
    assert result.id == 1


# ── add_issue_comment caretaker-marker cap ───────────────────────────────────


def _ck(i: int, body: str = "<!-- caretaker:status -->\n"):
    """Helper for short caretaker-marker comment fixtures used in cap tests."""
    from caretaker.github_client.models import Comment, User

    return Comment(
        id=i,
        body=body,
        user=User(login="b", id=0, type="Bot"),
        created_at=_TS,
    )


@pytest.mark.asyncio
async def test_add_issue_comment_below_cap_posts_normally() -> None:
    client = GitHubClient(
        credentials_provider=StaticCredentialsProvider(default_token="x"),
        comment_cap_per_issue=5,
    )
    body = "<!-- caretaker:test -->\nbody"
    existing = [_ck(i) for i in range(3)]  # 3 < 5 cap

    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=existing)),
        patch.object(
            client,
            "_post",
            AsyncMock(
                return_value={
                    "id": 99,
                    "body": body,
                    "user": {"login": "b", "id": 0, "type": "Bot"},
                    "created_at": _TS.isoformat(),
                }
            ),
        ),
    ):
        result = await client.add_issue_comment("o", "r", 1, body)
    assert result.id == 99


@pytest.mark.asyncio
async def test_add_issue_comment_at_cap_refuses_with_marker_body() -> None:
    client = GitHubClient(
        credentials_provider=StaticCredentialsProvider(default_token="x"),
        comment_cap_per_issue=3,
    )
    body = "<!-- caretaker:test -->\nbody"
    existing = [_ck(i) for i in range(3)]  # exactly cap

    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=existing)),
        patch.object(client, "_post", AsyncMock()) as post_mock,
        pytest.raises(GitHubAPIError, match="cap 3"),
    ):
        await client.add_issue_comment("o", "r", 1, body)
    post_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_issue_comment_cap_does_not_apply_to_non_caretaker_body() -> None:
    """Human comments and non-caretaker bot comments are never blocked."""
    client = GitHubClient(
        credentials_provider=StaticCredentialsProvider(default_token="x"),
        comment_cap_per_issue=3,
    )
    body = "Just a normal comment"  # no caretaker marker
    existing = [_ck(i) for i in range(10)]  # way over cap, body has no marker
    posted_payload = {
        "id": 100,
        "body": body,
        "user": {"login": "b", "id": 0, "type": "Bot"},
        "created_at": _TS.isoformat(),
    }
    with (
        patch.object(client, "get_pr_comments", AsyncMock(return_value=existing)),
        patch.object(client, "_post", AsyncMock(return_value=posted_payload)),
    ):
        result = await client.add_issue_comment("o", "r", 1, body)
    assert result.id == 100


@pytest.mark.asyncio
async def test_add_issue_comment_cap_zero_disables_check() -> None:

    client = GitHubClient(
        credentials_provider=StaticCredentialsProvider(default_token="x"),
        comment_cap_per_issue=0,  # disabled
    )
    body = "<!-- caretaker:test -->\nbody"
    posted_payload = {
        "id": 5,
        "body": body,
        "user": {"login": "b", "id": 0, "type": "Bot"},
        "created_at": _TS.isoformat(),
    }
    with (
        patch.object(client, "get_pr_comments", AsyncMock()) as get_mock,
        patch.object(client, "_post", AsyncMock(return_value=posted_payload)),
    ):
        await client.add_issue_comment("o", "r", 1, body)
    # When cap is disabled, we should NOT even bother fetching existing comments
    get_mock.assert_not_awaited()
