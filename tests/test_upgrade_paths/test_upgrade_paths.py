"""Upgrade path simulation tests across representative versions."""

from __future__ import annotations

from caretaker.upgrade_agent.release_checker import Release, needs_upgrade


class TestUpgradePaths:
    def test_patch_upgrade_path(self) -> None:
        target = Release(
            version="1.0.1",
            min_compatible="1.0.0",
            changelog_url="https://example.com/v1.0.1",
        )
        assert needs_upgrade("1.0.0", target) is True

    def test_minor_upgrade_path(self) -> None:
        target = Release(
            version="1.2.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/v1.2.0",
        )
        assert needs_upgrade("1.1.5", target) is True

    def test_major_upgrade_path(self) -> None:
        target = Release(
            version="2.0.0",
            min_compatible="2.0.0",
            changelog_url="https://example.com/v2.0.0",
            breaking=True,
        )
        assert needs_upgrade("1.9.9", target) is True

    def test_no_upgrade_path_when_up_to_date(self) -> None:
        target = Release(
            version="2.0.0",
            min_compatible="2.0.0",
            changelog_url="https://example.com/v2.0.0",
        )
        assert needs_upgrade("2.0.0", target) is False
