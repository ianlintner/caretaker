"""Tests for release checker functionality."""

from __future__ import annotations

import httpx
import pytest
import respx

from caretaker.upgrade_agent.release_checker import (
    GITHUB_RELEASES_API,
    RELEASES_JSON_URL,
    Release,
    _fetch_releases_json,
    fetch_releases,
    needs_upgrade,
)


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


# GitHub Releases API response payloads -----------------------------------


_GH_RELEASES_RESPONSE = [
    {
        "tag_name": "v1.3.0",
        "html_url": "https://github.com/owner/repo/releases/tag/v1.3.0",
        "body": "Bug fixes and improvements.",
        "draft": False,
        "prerelease": False,
    },
    {
        "tag_name": "v1.2.0",
        "html_url": "https://github.com/owner/repo/releases/tag/v1.2.0",
        "body": "min-compatible: 1.0.0\nBreaking changes in API.",
        "draft": False,
        "prerelease": False,
    },
    {
        "tag_name": "v1.1.0-rc1",
        "html_url": "https://github.com/owner/repo/releases/tag/v1.1.0-rc1",
        "body": "Release candidate.",
        "draft": False,
        "prerelease": True,  # should be skipped
    },
    {
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/owner/repo/releases/tag/v1.0.0",
        "body": "",
        "draft": True,  # should be skipped
        "prerelease": False,
    },
]

_RELEASES_JSON_PAYLOAD = {
    "releases": [
        {
            "version": "1.3.0",
            "min_compatible": "1.0.0",
            "changelog_url": "https://example.com/v1.3.0",
            "upgrade_notes": "Update pins.",
            "breaking": False,
        }
    ]
}


@pytest.mark.asyncio
class TestFetchReleases:
    @respx.mock
    async def test_parses_github_releases(self) -> None:
        url = GITHUB_RELEASES_API.format(owner="ianlintner", repo="caretaker")
        respx.get(url).mock(return_value=httpx.Response(200, json=_GH_RELEASES_RESPONSE))

        releases = await fetch_releases()

        # Drafts and pre-releases must be filtered out.
        assert len(releases) == 2
        assert releases[0].version == "1.3.0"
        assert releases[0].changelog_url == "https://github.com/owner/repo/releases/tag/v1.3.0"
        assert releases[0].breaking is False
        # min_compatible defaults to the version itself when not in body.
        assert releases[0].min_compatible == "1.3.0"

    @respx.mock
    async def test_parses_min_compatible_and_breaking(self) -> None:
        url = GITHUB_RELEASES_API.format(owner="ianlintner", repo="caretaker")
        respx.get(url).mock(return_value=httpx.Response(200, json=_GH_RELEASES_RESPONSE))

        releases = await fetch_releases()

        # Second non-draft/non-prerelease is v1.2.0.
        r = releases[1]
        assert r.version == "1.2.0"
        assert r.min_compatible == "1.0.0"
        assert r.breaking is True

    @respx.mock
    async def test_falls_back_to_releases_json_on_api_error(self) -> None:
        gh_url = GITHUB_RELEASES_API.format(owner="ianlintner", repo="caretaker")
        json_url = RELEASES_JSON_URL.format(owner="ianlintner", repo="caretaker")
        respx.get(gh_url).mock(return_value=httpx.Response(500))
        respx.get(json_url).mock(return_value=httpx.Response(200, json=_RELEASES_JSON_PAYLOAD))

        releases = await fetch_releases()

        assert len(releases) == 1
        assert releases[0].version == "1.3.0"
        assert releases[0].upgrade_notes == "Update pins."

    @respx.mock
    async def test_returns_empty_list_when_both_sources_fail(self) -> None:
        gh_url = GITHUB_RELEASES_API.format(owner="ianlintner", repo="caretaker")
        json_url = RELEASES_JSON_URL.format(owner="ianlintner", repo="caretaker")
        respx.get(gh_url).mock(return_value=httpx.Response(500))
        respx.get(json_url).mock(return_value=httpx.Response(404))

        releases = await fetch_releases()

        assert releases == []

    @respx.mock
    async def test_skips_entries_with_missing_tag(self) -> None:
        url = GITHUB_RELEASES_API.format(owner="ianlintner", repo="caretaker")
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "tag_name": "",
                        "html_url": "",
                        "body": "",
                        "draft": False,
                        "prerelease": False,
                    }
                ],
            )
        )

        releases = await fetch_releases()

        assert releases == []


@pytest.mark.asyncio
class TestFetchReleasesJson:
    @respx.mock
    async def test_parses_legacy_manifest(self) -> None:
        url = RELEASES_JSON_URL.format(owner="ianlintner", repo="caretaker")
        respx.get(url).mock(return_value=httpx.Response(200, json=_RELEASES_JSON_PAYLOAD))

        releases = await _fetch_releases_json()

        assert len(releases) == 1
        r = releases[0]
        assert r.version == "1.3.0"
        assert r.min_compatible == "1.0.0"
        assert r.upgrade_notes == "Update pins."
        assert r.breaking is False

    @respx.mock
    async def test_returns_empty_on_http_error(self) -> None:
        url = RELEASES_JSON_URL.format(owner="ianlintner", repo="caretaker")
        respx.get(url).mock(return_value=httpx.Response(503))

        releases = await _fetch_releases_json()

        assert releases == []
