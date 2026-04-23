"""Tests for UpgradeAgent run logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from caretaker.config import UpgradeAgentConfig
from caretaker.github_client.models import PullRequest
from caretaker.upgrade_agent.agent import UpgradeAgent
from caretaker.upgrade_agent.release_checker import Release


@pytest.mark.asyncio
class TestUpgradeAgent:
    async def test_disabled_agent_noop(self) -> None:
        github = AsyncMock()
        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=False),
            current_version="1.0.0",
        )

        report = await agent.run()

        assert report.checked is False
        assert report.upgrade_needed is False

    async def test_creates_upgrade_issue_when_needed(self) -> None:
        github = AsyncMock()
        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=True),
            current_version="1.0.0",
        )
        release = Release(
            version="1.1.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )

        issue_mock = AsyncMock(return_value=99)
        with (
            patch(
                "caretaker.upgrade_agent.agent.fetch_releases",
                new=AsyncMock(return_value=[release]),
            ),
            patch.object(agent._planner, "create_upgrade_issue", new=issue_mock),
        ):
            report = await agent.run()

        assert report.checked is True
        assert report.upgrade_needed is True
        assert report.latest_version == "1.1.0"
        assert report.upgrade_issue == 99

    async def test_no_upgrade_when_current_latest(self) -> None:
        github = AsyncMock()
        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=True),
            current_version="1.1.0",
        )
        release = Release(
            version="1.1.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )

        releases_mock = AsyncMock(return_value=[release])
        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=releases_mock):
            report = await agent.run()

        assert report.checked is True
        assert report.upgrade_needed is False
        assert report.upgrade_issue is None

    async def test_auto_ready_drafts_default_is_true(self) -> None:
        cfg = UpgradeAgentConfig()
        assert cfg.auto_ready_drafts is True

    async def test_auto_ready_drafts_promotes_passing_copilot_draft(self) -> None:
        """When auto_ready_drafts=True and a Copilot draft PR has CI green, it is readied."""
        github = AsyncMock()
        draft_pr = PullRequest(
            number=7,
            title="Upgrade to v1.1.0",
            head_sha="abc123",
            draft=True,
            node_id="PR_kwDOABC123",
            user_login="copilot-swe-agent[bot]",
        )
        github.list_pull_requests = AsyncMock(return_value=[draft_pr])
        github.get_combined_status = AsyncMock(return_value="success")
        github.mark_pull_request_ready = AsyncMock(return_value=True)

        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=True, auto_ready_drafts=True),
            current_version="1.1.0",
        )
        release = Release(version="1.1.0", min_compatible="1.0.0", changelog_url="")
        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=AsyncMock(return_value=[release])):
            report = await agent.run()

        github.mark_pull_request_ready.assert_awaited_once_with("PR_kwDOABC123")
        assert report.readied_draft_prs == [7]

    async def test_auto_ready_drafts_disabled_skips_draft_promotion(self) -> None:
        """When auto_ready_drafts=False, draft PRs are not touched."""
        github = AsyncMock()
        draft_pr = PullRequest(
            number=8,
            title="Upgrade to v1.1.0",
            head_sha="def456",
            draft=True,
            node_id="PR_kwDODEF456",
            user_login="copilot-swe-agent[bot]",
        )
        github.list_pull_requests = AsyncMock(return_value=[draft_pr])
        github.get_combined_status = AsyncMock(return_value="success")
        github.mark_pull_request_ready = AsyncMock(return_value=True)

        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=True, auto_ready_drafts=False),
            current_version="1.1.0",
        )
        release = Release(version="1.1.0", min_compatible="1.0.0", changelog_url="")
        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=AsyncMock(return_value=[release])):
            report = await agent.run()

        github.mark_pull_request_ready.assert_not_awaited()
        assert report.readied_draft_prs == []

    async def test_auto_ready_drafts_non_draft_pr_not_touched(self) -> None:
        """Non-draft PRs are not affected even if CI is green."""
        github = AsyncMock()
        ready_pr = PullRequest(
            number=9,
            title="Upgrade to v1.1.0",
            head_sha="ghi789",
            draft=False,
            node_id="PR_kwDOGHI789",
            user_login="copilot-swe-agent[bot]",
        )
        github.list_pull_requests = AsyncMock(return_value=[ready_pr])
        github.get_combined_status = AsyncMock(return_value="success")
        github.mark_pull_request_ready = AsyncMock(return_value=True)

        agent = UpgradeAgent(
            github=github,
            owner="o",
            repo="r",
            config=UpgradeAgentConfig(enabled=True, auto_ready_drafts=True),
            current_version="1.1.0",
        )
        release = Release(version="1.1.0", min_compatible="1.0.0", changelog_url="")
        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=AsyncMock(return_value=[release])):
            report = await agent.run()

        github.mark_pull_request_ready.assert_not_awaited()
        assert report.readied_draft_prs == []
