"""Tests for GitHubClient — focusing on error-handling branches."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def _make_client() -> GitHubClient:
    """Return a GitHubClient with a fake token (no real HTTP calls)."""
    return GitHubClient(token="fake-token")


@pytest.mark.asyncio
class TestAssignCopilotToIssue:
    async def test_succeeds_on_200(self) -> None:
        """assign_copilot_to_issue completes without error on a successful response."""
        client = _make_client()
        with patch.object(client, "_copilot_post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"assignees": ["copilot-swe-agent[bot]"]}
            # Should not raise
            await client.assign_copilot_to_issue("owner", "repo", 1)
        mock_post.assert_awaited_once()

    async def test_swallows_403(self) -> None:
        """A 403 from the assignees endpoint is logged as a warning, not raised."""
        client = _make_client()
        with patch.object(client, "_copilot_post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = GitHubAPIError(403, "Forbidden")
            # Should not raise — just warn
            await client.assign_copilot_to_issue("owner", "repo", 42)

    async def test_swallows_422(self) -> None:
        """A 422 (Unprocessable Entity) is treated the same as 403 — warning only."""
        client = _make_client()
        with patch.object(client, "_copilot_post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = GitHubAPIError(422, "Unprocessable Entity")
            await client.assign_copilot_to_issue("owner", "repo", 7)

    async def test_re_raises_other_errors(self) -> None:
        """Errors other than 403/422 are still propagated to the caller."""
        client = _make_client()
        with patch.object(client, "_copilot_post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = GitHubAPIError(500, "Internal Server Error")
            with pytest.raises(GitHubAPIError) as exc_info:
                await client.assign_copilot_to_issue("owner", "repo", 3)
            assert exc_info.value.status_code == 500
