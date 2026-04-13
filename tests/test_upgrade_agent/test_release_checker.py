"""Tests for release checker functionality."""

from __future__ import annotations

import pytest

from caretaker.upgrade_agent.release_checker import Release, needs_upgrade


class TestNeedsUpgrade:
    def test_current_behind_latest(self) -> None:
        latest = Release(
            version="1.2.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        assert needs_upgrade("1.1.0", latest) is True

    def test_current_equal_latest(self) -> None:
        latest = Release(
            version="1.2.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        assert needs_upgrade("1.2.0", latest) is False

    def test_current_ahead_latest(self) -> None:
        latest = Release(
            version="1.2.0",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        assert needs_upgrade("1.3.0", latest) is False

    def test_invalid_version_returns_false(self) -> None:
        latest = Release(
            version="bad-version",
            min_compatible="1.0.0",
            changelog_url="https://example.com/changelog",
        )
        assert needs_upgrade("1.2.0", latest) is False
