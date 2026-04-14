"""Tests for GitHub API client behavior."""

from __future__ import annotations

import httpx
import pytest
import respx

from caretaker.github_client.api import API_BASE, GitHubAPIError, GitHubClient
from caretaker.github_client.models import COPILOT_ASSIGNEE_LOGIN


@pytest.mark.asyncio
class TestGitHubClient:
    async def test_assign_copilot_to_issue_uses_rest_assignees_api(self) -> None:
        async with GitHubClient(token="test-token") as github:
            with respx.mock(base_url=API_BASE) as router:
                assignees = router.post("/repos/ianlintner/caretaker/issues/24/assignees").mock(
                    return_value=httpx.Response(
                        201,
                        json={
                            "assignees": [
                                {
                                    "login": COPILOT_ASSIGNEE_LOGIN,
                                    "id": 1,
                                }
                            ]
                        },
                    ),
                )

                await github.assign_copilot_to_issue("ianlintner", "caretaker", 24)

                assert assignees.called
                assert assignees.calls[0].request.content == (
                    b'{"assignees":["copilot-swe-agent"]}'
                )

    async def test_assign_copilot_to_issue_propagates_rest_errors(self) -> None:
        async with GitHubClient(token="test-token") as github:
            with respx.mock(base_url=API_BASE) as router:
                router.post("/repos/ianlintner/caretaker/issues/24/assignees").mock(
                    return_value=httpx.Response(
                        422,
                        text="Validation Failed",
                    )
                )

                with pytest.raises(GitHubAPIError, match="Validation Failed"):
                    await github.assign_copilot_to_issue("ianlintner", "caretaker", 24)
