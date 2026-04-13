"""Tests for UpgradeAgent run logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from caretaker.config import UpgradeAgentConfig
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

        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=AsyncMock(return_value=[release])):
            with patch.object(agent._planner, "create_upgrade_issue", new=AsyncMock(return_value=99)):
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

        with patch("caretaker.upgrade_agent.agent.fetch_releases", new=AsyncMock(return_value=[release])):
            report = await agent.run()

        assert report.checked is True
        assert report.upgrade_needed is False
        assert report.upgrade_issue is None
