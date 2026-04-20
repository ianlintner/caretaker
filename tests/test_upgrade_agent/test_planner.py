"""Tests for upgrade planner."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.models import Issue, PRState, PullRequest, User
from caretaker.upgrade_agent.planner import (
    SYNC_FILES,
    UpgradePlanner,
    build_sync_issue_body,
    build_upgrade_issue_body,
)
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
        assert "caretaker:causal" in body
        assert "source=upgrade" in body

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

    async def test_does_not_recreate_issue_when_previous_is_closed(self) -> None:
        """A closed upgrade issue for this version must prevent a new one being opened.

        This is the root cause of the duplicate-Copilot-PR problem: when an
        upgrade issue was closed (e.g. the PR failed) caretaker previously
        ignored it and created a fresh issue, causing @copilot to open yet
        another PR.  With state="all" in the issues query the closed issue is
        found and re-used instead.
        """
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="1.5.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        closed_issue = make_issue(10, "Upgrade to v1.5.0", maintainer=True)
        closed_issue.state = "closed"
        github.list_issues.return_value = [closed_issue]

        number = await planner.create_upgrade_issue("1.4.0", target)

        assert number == 10
        github.create_issue.assert_not_called()
        # Confirm the query was made with state="all"
        github.list_issues.assert_awaited_once()
        call_kwargs = github.list_issues.call_args.kwargs
        assert call_kwargs.get("state") == "all"

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


class TestBuildSyncIssueBody:
    def test_body_contains_version_and_marker(self) -> None:
        body = build_sync_issue_body("1.5.0")
        assert "Sync installation files to v1.5.0" in body
        assert "VERSION: 1.5.0" in body
        assert "<!-- caretaker:sync -->" in body
        assert "<!-- /caretaker:sync -->" in body

    def test_body_lists_all_sync_files(self) -> None:
        body = build_sync_issue_body("1.5.0")
        for local_path, _template_path in SYNC_FILES:
            assert local_path in body

    def test_body_contains_template_urls_with_tag(self) -> None:
        body = build_sync_issue_body("2.0.0")
        assert "v2.0.0" in body
        for _local_path, template_path in SYNC_FILES:
            assert template_path in body

    def test_body_contains_copilot_mention(self) -> None:
        body = build_sync_issue_body("1.0.0")
        assert "@copilot" in body

    def test_body_contains_acceptance_criteria(self) -> None:
        body = build_sync_issue_body("1.0.0")
        assert "Acceptance criteria" in body
        assert "All tests pass" in body


@pytest.mark.asyncio
class TestUpgradePlannerSync:
    async def test_reuses_existing_sync_issue(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_issues.return_value = [
            make_issue(20, "Sync installation files to v1.5.0", maintainer=True),
        ]

        number = await planner.create_sync_issue("1.5.0")

        assert number == 20
        github.create_issue.assert_not_called()

    async def test_creates_new_sync_issue(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_issues.return_value = []
        github.create_issue.return_value = make_issue(55, "Sync installation files to v1.5.0")

        number = await planner.create_sync_issue("1.5.0")

        assert number == 55
        github.create_issue.assert_awaited_once()


@pytest.mark.asyncio
class TestUpgradeIssueMarkerDedupe:
    """Closes the rust-oauth2-server #118/#121/#126/#129/#153 dupe pattern.

    Title-substring dedupe failed when two upgrades targeted similar versions
    (e.g. v0.5.0 and v0.5.2 both substring-match each other in some lookups).
    The new behavior is marker-first, title-second with backfill.
    """

    async def test_dedupes_by_body_marker_when_title_differs(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="0.5.0",
            min_compatible="0.1.0",
            changelog_url="https://example.com/changelog",
        )

        existing = make_issue(101, "Some custom title that doesn't mention version")
        existing.body = "preamble\n\n<!-- caretaker:upgrade target=0.5.0 -->\n"
        github.list_issues.return_value = [existing]

        number = await planner.create_upgrade_issue("0.4.0", target)

        assert number == 101
        github.create_issue.assert_not_called()

    async def test_new_issue_body_carries_marker(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="0.10.0",
            min_compatible="0.10.0",
            changelog_url="https://example.com/changelog",
        )
        github.list_issues.return_value = []
        github.create_issue.return_value = make_issue(77, "Upgrade to v0.10.0")

        await planner.create_upgrade_issue("0.9.0", target)

        body = github.create_issue.call_args.kwargs["body"]
        assert "<!-- caretaker:upgrade target=0.10.0 -->" in body

    async def test_legacy_title_match_backfills_marker(self) -> None:
        """Issues created before the marker existed get the marker added.

        Prevents the next dedupe lookup from missing the older issue when
        title text rotates (which is what allowed multiple v0.5.0 issues
        to be opened against rust-oauth2-server).
        """
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="0.5.0",
            min_compatible="0.1.0",
            changelog_url="https://example.com/changelog",
        )

        legacy_issue = make_issue(33, "Upgrade to v0.5.0", maintainer=True)
        legacy_issue.body = "old body without marker"
        github.list_issues.return_value = [legacy_issue]

        number = await planner.create_upgrade_issue("0.4.0", target)

        assert number == 33
        github.create_issue.assert_not_called()
        # The legacy issue's body should be updated to carry the marker
        github.update_issue.assert_awaited_once()
        update_kwargs = github.update_issue.call_args.kwargs
        assert "<!-- caretaker:upgrade target=0.5.0 -->" in update_kwargs.get("body", "")

    async def test_marker_takes_precedence_over_title_match(self) -> None:
        """When both markers exist, the marker-keyed issue wins (precise match)."""
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        target = Release(
            version="0.5.2",
            min_compatible="0.1.0",
            changelog_url="https://example.com/changelog",
        )

        # Title match for 0.5.2, but marker is for 0.5.0 — should NOT dedupe
        title_only = make_issue(40, "Upgrade to v0.5.2", maintainer=True)
        title_only.body = "<!-- caretaker:upgrade target=0.5.0 -->"
        github.list_issues.return_value = [title_only]
        github.create_issue.return_value = make_issue(50, "Upgrade to v0.5.2")

        number = await planner.create_upgrade_issue("0.5.0", target)

        # New issue must be created — the existing one targets a different version
        # despite the misleading title.
        assert number == 50
        github.create_issue.assert_awaited_once()


def make_pr(number: int, title: str) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        state=PRState.OPEN,
        user=User(login="copilot-swe-agent[bot]", id=1, type="Bot"),
    )


@pytest.mark.asyncio
class TestCloseSupersededUpgradePRs:
    """Portfolio #144/#146 and rust-oauth2-server pattern: two Copilot PRs
    targeting the same upgrade version racing each other. Keep newest, close older.
    """

    async def test_no_prs_returns_empty(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_pull_requests.return_value = []

        closed = await planner.close_superseded_upgrade_prs("0.10.0")

        assert closed == []
        github.update_issue.assert_not_called()

    async def test_single_pr_not_closed(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_pull_requests.return_value = [make_pr(50, "Upgrade to v0.10.0")]

        closed = await planner.close_superseded_upgrade_prs("0.10.0")

        assert closed == []
        github.update_issue.assert_not_called()

    async def test_closes_older_prs_keeps_newest(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_pull_requests.return_value = [
            make_pr(10, "Upgrade to v0.10.0"),
            make_pr(20, "Upgrade to v0.10.0"),
            make_pr(30, "Upgrade to v0.10.0"),
        ]

        closed = await planner.close_superseded_upgrade_prs("0.10.0")

        assert closed == [10, 20]
        assert github.update_issue.await_count == 2
        closed_numbers = [call.args[2] for call in github.update_issue.await_args_list]
        assert set(closed_numbers) == {10, 20}
        for call in github.update_issue.await_args_list:
            assert call.kwargs == {"state": "closed"}
        # Superseded comment references the keeper.
        assert github.add_issue_comment.await_count == 2
        for call in github.add_issue_comment.await_args_list:
            assert "#30" in call.args[3]

    async def test_ignores_prs_for_different_version(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        github.list_pull_requests.return_value = [
            make_pr(10, "Upgrade to v0.9.0"),
            make_pr(20, "Upgrade to v0.10.0"),
        ]

        closed = await planner.close_superseded_upgrade_prs("0.10.0")

        assert closed == []
        github.update_issue.assert_not_called()


@pytest.mark.asyncio
class TestSyncIssueMarkerDedupe:
    async def test_dedupes_by_marker(self) -> None:
        github = AsyncMock()
        planner = UpgradePlanner(github=github, owner="o", repo="r")
        existing = make_issue(99, "Some other title")
        existing.body = "<!-- caretaker:sync target=1.5.0 -->"
        github.list_issues.return_value = [existing]

        number = await planner.create_sync_issue("1.5.0")

        assert number == 99
        github.create_issue.assert_not_called()
