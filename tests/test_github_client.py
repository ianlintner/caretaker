"""Tests for GitHub client API — especially error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.api import GitHubAPIError, GitHubClient


def make_client() -> GitHubClient:
    """Create a GitHubClient with a dummy token (no real HTTP calls)."""
    return GitHubClient(token="fake-token")


@pytest.mark.asyncio
class TestAssignCopilotToIssue:
    """assign_copilot_to_issue should handle 403/422 gracefully."""

    async def test_403_is_logged_as_warning_not_raised(self) -> None:
        """A 403 Forbidden response must not propagate as an exception."""
        client = make_client()
        client._copilot_post = AsyncMock(  # type: ignore[method-assign]
            side_effect=GitHubAPIError(403, "Forbidden")
        )

        # Should not raise
        await client.assign_copilot_to_issue("owner", "repo", 1)

    async def test_422_is_logged_as_warning_not_raised(self) -> None:
        """A 422 Unprocessable Entity (e.g. invalid assignee) must not propagate."""
        client = make_client()
        client._copilot_post = AsyncMock(  # type: ignore[method-assign]
            side_effect=GitHubAPIError(422, "Unprocessable Entity")
        )

        # Should not raise
        await client.assign_copilot_to_issue("owner", "repo", 2)

    async def test_other_errors_are_still_raised(self) -> None:
        """Non-403/422 errors (e.g. 500) must still propagate."""
        client = make_client()
        client._copilot_post = AsyncMock(  # type: ignore[method-assign]
            side_effect=GitHubAPIError(500, "Internal Server Error")
        )

        with pytest.raises(GitHubAPIError) as exc_info:
            await client.assign_copilot_to_issue("owner", "repo", 3)

        assert exc_info.value.status_code == 500

    async def test_404_result_raises_error(self) -> None:
        """A None result (404 from underlying request) raises a GitHubAPIError."""
        client = make_client()
        client._copilot_post = AsyncMock(return_value=None)  # type: ignore[method-assign]

        with pytest.raises(GitHubAPIError) as exc_info:
            await client.assign_copilot_to_issue("owner", "repo", 4)

        assert exc_info.value.status_code == 404
