"""Integration-style tests for the guardrails wiring on
:class:`caretaker.github_client.api.GitHubClient`.

The filter/sanitize hooks are thin — these tests make sure the plumbing
actually runs on the hot path by intercepting the post-layer payloads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.api import GitHubClient


@pytest.mark.asyncio
async def test_add_issue_comment_strips_ansi_from_body() -> None:
    client = GitHubClient(token="dummy", comment_cap_per_issue=0)
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: Any | None = None, **kwargs: Any) -> Any:  # noqa: ARG001
        captured["path"] = path
        captured["json"] = json
        return {
            "id": 1,
            "user": {"login": "bot", "id": 2},
            "body": json["body"] if json else "",
            "created_at": datetime.now(UTC).isoformat(),
        }

    client._post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]

    # LLM-authored comment body that includes an ANSI colour-code payload.
    dirty_body = "Heads up: \x1b[31mALERT\x1b[0m — CI flaked."
    await client.add_issue_comment("o", "r", 5, dirty_body)

    assert "\x1b" not in captured["json"]["body"]
    assert "ALERT" in captured["json"]["body"]


@pytest.mark.asyncio
async def test_create_issue_filters_hidden_link_in_body() -> None:
    client = GitHubClient(token="dummy")
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: Any | None = None, **kwargs: Any) -> Any:  # noqa: ARG001
        captured.setdefault("path", path)
        captured.setdefault("json", json)
        return {
            "number": 42,
            "title": json["title"] if json else "",
            "body": json["body"] if json else "",
            "state": "open",
            "user": {"login": "bot", "id": 2},
            "labels": [],
            "assignees": [],
        }

    client._post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]

    dirty_body = "Please review [https://legit.example.com](https://attacker.test/p) for details."
    await client.create_issue("o", "r", "Title", dirty_body)

    posted_body: str = captured["json"]["body"]
    # The hidden-link rewriter keeps both the visible text and the target
    # so a reader can eyeball the deception.
    assert "attacker.test/p" in posted_body
    # And the raw Markdown deceptive link form is gone.
    assert "[https://legit.example.com](https://attacker.test/p)" not in posted_body


@pytest.mark.asyncio
async def test_add_issue_comment_preserves_legitimate_caretaker_marker() -> None:
    """Regression guard: the GitHub-client boundary filter must leave
    caretaker's own markers untouched (default policy), otherwise
    status-comment upserts break."""
    client = GitHubClient(token="dummy", comment_cap_per_issue=0)
    captured: dict[str, Any] = {}

    async def fake_post(path: str, json: Any | None = None, **kwargs: Any) -> Any:  # noqa: ARG001
        captured["json"] = json
        return {
            "id": 1,
            "user": {"login": "bot", "id": 2},
            "body": json["body"] if json else "",
            "created_at": datetime.now(UTC).isoformat(),
        }

    client._post = AsyncMock(side_effect=fake_post)  # type: ignore[method-assign]

    body = "## Caretaker status\n<!-- caretaker:status -->\nAll checks green."
    await client.add_issue_comment("o", "r", 1, body)

    assert "<!-- caretaker:status -->" in captured["json"]["body"]
