"""Release checker — polls GitHub Releases API for the latest caretaker release."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GITHUB_RELEASES_API = "https://api.github.com/repos/{owner}/{repo}/releases"
RELEASES_JSON_URL = "https://raw.githubusercontent.com/{owner}/{repo}/main/releases.json"
DEFAULT_OWNER = "ianlintner"
DEFAULT_REPO = "caretaker"

# Detect breaking-change markers in release body text.
_BREAKING_RE = re.compile(r"\bbreaking\b", re.IGNORECASE)
# Parse optional "min-compatible: x.y.z" annotation from release body.
_MIN_COMPAT_RE = re.compile(r"min[_-]compatible:\s*([\d.]+)", re.IGNORECASE)


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
    """Fetch releases from the GitHub Releases API.

    Falls back to the legacy ``releases.json`` manifest on failure.
    Draft and pre-release entries are skipped.
    """
    url = GITHUB_RELEASES_API.format(owner=owner, repo=repo)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(
                url,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
            data: list[dict[str, object]] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "Failed to fetch GitHub releases from %s: %s — falling back to releases.json",
                url,
                exc,
            )
            return await _fetch_releases_json(owner, repo)

    releases: list[Release] = []
    for entry in data:
        if entry.get("draft") or entry.get("prerelease"):
            continue
        tag = str(entry.get("tag_name", ""))
        version = tag.lstrip("v")
        if not version:
            continue
        body = str(entry.get("body") or "")
        min_compat_match = _MIN_COMPAT_RE.search(body)
        min_compatible = min_compat_match.group(1) if min_compat_match else version
        releases.append(
            Release(
                version=version,
                min_compatible=min_compatible,
                changelog_url=str(entry.get("html_url", "")),
                upgrade_notes=body if body else None,
                breaking=bool(_BREAKING_RE.search(body)),
            )
        )
    return releases


async def _fetch_releases_json(
    owner: str = DEFAULT_OWNER,
    repo: str = DEFAULT_REPO,
) -> list[Release]:
    """Fallback: parse the legacy ``releases.json`` manifest."""
    url = RELEASES_JSON_URL.format(owner=owner, repo=repo)
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Failed to fetch releases.json from %s: %s", url, exc)
            return []

    releases: list[Release] = []
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
    """Return ``True`` if *current_version* is older than *latest*."""
    from packaging.version import InvalidVersion, Version

    try:
        current = Version(current_version)
        latest_v = Version(latest.version)
    except InvalidVersion:
        logger.warning("Invalid version format: %s or %s", current_version, latest.version)
        return False

    return current < latest_v
