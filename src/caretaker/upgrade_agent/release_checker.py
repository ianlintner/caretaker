"""Release checker — polls the central releases manifest."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

RELEASES_URL = "https://raw.githubusercontent.com/{owner}/{repo}/main/releases.json"
DEFAULT_OWNER = "ianlintner"
DEFAULT_REPO = "caretaker"


@dataclass
class Release:
    version: str
    min_compatible: str
    changelog_url: str
    upgrade_notes: str | None = None
    breaking: bool = False


async def fetch_releases(
    owner: str = DEFAULT_OWNER,
    repo: str = DEFAULT_REPO,
) -> list[Release]:
    """Fetch the releases manifest from the central repo."""
    url = RELEASES_URL.format(owner=owner, repo=repo)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Failed to fetch releases from %s: %s", url, exc)
            return []

    releases = []
    for entry in data.get("releases", []):
        releases.append(
            Release(
                version=entry["version"],
                min_compatible=entry.get("min_compatible", entry["version"]),
                changelog_url=entry.get("changelog_url", ""),
                upgrade_notes=entry.get("upgrade_notes"),
                breaking=entry.get("breaking", False),
            )
        )
    return releases


def needs_upgrade(current_version: str, latest: Release) -> bool:
    """Check if the current version is behind the latest release."""
    from packaging.version import InvalidVersion, Version

    try:
        current = Version(current_version)
        latest_v = Version(latest.version)
    except InvalidVersion:
        logger.warning("Invalid version format: %s or %s", current_version, latest.version)
        return False

    return current < latest_v
