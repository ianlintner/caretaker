"""Tests for GitHub API client behavior."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from caretaker.github_client.api import API_BASE, GitHubAPIError, GitHubClient


@pytest.mark.asyncio
class TestGitHubClient:
    async def test_assign_copilot_to_issue_uses_graphql_assignable_ids(self) -> None:
        async with GitHubClient(token="test-token") as github:
            with respx.mock(base_url=API_BASE) as router:
                graphql = router.post("/graphql")
                graphql.side_effect = [
                    httpx.Response(
                        200,
                        json={
                            "data": {
                                "repository": {
                                    "issue": {"id": "ISSUE_1"},
                                    "suggestedActors": {
                                        "nodes": [
                                            {"login": "copilot-swe-agent", "id": "BOT_1"},
                                            {"login": "ianlintner", "id": "USER_1"},
                                        ]
                                    },
                                }
                            }
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": {
                                "addAssigneesToAssignable": {
                                    "assignable": {
                                        "assignees": {
                                            "nodes": [{"login": "copilot-swe-agent", "id": "BOT_1"}]
                                        }
                                    }
                                }
                            }
                        },
                    ),
                ]

                await github.assign_copilot_to_issue("ianlintner", "caretaker", 24)

                assert graphql.call_count == 2
                query_payload = json.loads(graphql.calls[0].request.content)
                mutation_payload = json.loads(graphql.calls[1].request.content)
                assert query_payload["variables"]["number"] == 24
                assert mutation_payload["variables"] == {
                    "issueId": "ISSUE_1",
                    "assigneeIds": ["BOT_1"],
                }

    async def test_assign_copilot_to_issue_raises_when_copilot_not_assignable(self) -> None:
        async with GitHubClient(token="test-token") as github:
            with respx.mock(base_url=API_BASE) as router:
                router.post("/graphql").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "data": {
                                "repository": {
                                    "issue": {"id": "ISSUE_1"},
                                    "suggestedActors": {
                                        "nodes": [{"login": "ianlintner", "id": "USER_1"}]
                                    },
                                }
                            }
                        },
                    )
                )

                with pytest.raises(GitHubAPIError, match="Copilot is not assignable"):
                    await github.assign_copilot_to_issue("ianlintner", "caretaker", 24)

    async def test_assign_copilot_to_issue_raises_when_assignment_does_not_stick(self) -> None:
        async with GitHubClient(token="test-token") as github:
            with respx.mock(base_url=API_BASE) as router:
                graphql = router.post("/graphql")
                graphql.side_effect = [
                    httpx.Response(
                        200,
                        json={
                            "data": {
                                "repository": {
                                    "issue": {"id": "ISSUE_1"},
                                    "suggestedActors": {
                                        "nodes": [{"login": "copilot-swe-agent", "id": "BOT_1"}]
                                    },
                                }
                            }
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": {
                                "addAssigneesToAssignable": {
                                    "assignable": {"assignees": {"nodes": []}}
                                }
                            }
                        },
                    ),
                ]

                with pytest.raises(GitHubAPIError, match="did not stick"):
                    await github.assign_copilot_to_issue("ianlintner", "caretaker", 24)
