"""Tests for upgrade planner."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.models import Issue, User
from caretaker.upgrade_agent.planner import UpgradePlanner, build_upgrade_issue_body
from caretaker.upgrade_agent.release_checker import Release


def make_issue(number: int, title: str, maintainer: bool = True) -> Issue:
    issue = Issue(
        number=number,
        title=title,
        body="",
        user=User(login="bot", id=1, type="Bot"),
    )
    if maintainer:
        issue.title = f"[Maintainer] {title}"
    return issue


class TestBuildUpgradeIssueBody:
    def test_non_breaking_body_contains_expected_sections(self) -> None:
        release = Release(
            version="1.5.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
            upgrade_notes="No breaking changes.",
            breaking=False,
        )
        body = build_upgrade_issue_body("1.4.0", release)
        assert "Upgrade to v1.5.0" in body
        assert "BREAKING: False" in body
        assert "@copilot" in body

    def test_breaking_body_marks_warning(self) -> None:
        release = Release(
            version="2.0.0",
            min_compatible="2.0.0",
            changelog_url="https://example.com/changelog",
            breaking=True,
        )
        body = build_upgrade_issue_body("1.9.0", release)
        assert "breaking release" in body.lower()
        assert "BREAKING: True" in body


@pytest.mark.asyncio
class TestUpgradePlanner:
    async def test_reuses_existing_upgrade_issue(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="1.5.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        github.list_issues.return_value = [
            make_issue(10, "Upgrade to v1.5.0", maintainer=True),
        ]

        number = await planner.create_upgrade_issue("1.4.0", target)

        assert number == 10
        github.create_issue.assert_not_called()

    async def test_creates_new_issue_for_upgrade(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="1.5.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
            breaking=False,
        )
        github.list_issues.return_value = []
        github.create_issue.return_value = make_issue(42, "Upgrade to v1.5.0")

        number = await planner.create_upgrade_issue("1.4.0", target)

        assert number == 42
        github.create_issue.assert_awaited_once()
