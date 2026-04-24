"""PyPI-existence check for pinned caretaker versions used by fleet repos.

Root cause captured by this module
----------------------------------

``space-tycoon`` PR #15 maintainer job failed with

    ERROR: Cannot install caretaker-github @ git+...@v0.7.2 because these
    package versions have conflicting dependencies:
    inconsistent name: expected 'caretaker-github', but metadata has 'caretaker'

Two compounding problems:

1. ``v0.7.2`` had been yanked from PyPI (no longer a valid install
   target for ``pip install caretaker-github @ git+…``), and
2. at that tag the package on PyPI was still named ``caretaker`` not
   ``caretaker-github`` — so even bypassing the yank, the install
   would reject the name mismatch.

``doctor.check_version_pin`` catches "missing file" and "not a semver"
but validates format *only*. This module adds the two missing checks:

* Is the pinned version actually published (and not yanked) on PyPI
  under the expected package name?
* Across a fleet of consumer repos, which ones are drifting onto
  versions that no longer exist?

Both checks are **network-dependent and intentionally opt-in** — the
offline ``bootstrap-check`` flow must stay deterministic. Shepherd and
the doctor's extended (non-bootstrap) path are the intended callers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


#: Default PyPI JSON endpoint for package metadata. Released as a
#: constant so tests can substitute a local fixture server without
#: touching the calling code.
PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


#: The current package name under which caretaker publishes. Older
#: releases used plain ``caretaker`` — see the space-tycoon root cause
#: above — but everything from 0.8.1 onward is ``caretaker-github``.
DEFAULT_CARETAKER_PACKAGE = "caretaker-github"


@dataclass(frozen=True)
class PyPIVersionStatus:
    """Result of resolving a single ``(package, version)`` against PyPI.

    ``exists`` is True when the version appears as a release in PyPI's
    metadata and at least one wheel/sdist is not yanked. ``yanked`` is
    True when PyPI has the version but marked every artefact for that
    release as yanked — the pip install will fail with a warning that
    caretaker's self-heal ladder won't catch.
    """

    package: str
    version: str
    exists: bool
    yanked: bool
    reason: str
    # Convenience: the set of versions PyPI currently publishes for
    # this package, so shepherd/doctor can suggest a newer pin in one
    # round-trip. Empty on transport failures.
    available: tuple[str, ...] = ()


@dataclass
class VersionDriftReport:
    """Aggregate drift across a set of consumer repos.

    ``repos`` maps ``owner/repo`` → the version status we resolved.
    ``recommended_version`` is the newest non-yanked version on PyPI
    at the time of the scan; shepherd's auto-bump PR step uses it as
    the target when it decides to open a fix.
    """

    recommended_version: str | None = None
    repos: dict[str, PyPIVersionStatus] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def drifting_repos(self) -> list[str]:
        """Repos whose pinned version is not currently installable."""
        return [repo for repo, status in self.repos.items() if not status.exists or status.yanked]


async def fetch_pypi_package_info(
    package: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Fetch the PyPI JSON blob for ``package``.

    Returns None on transport errors or non-200 responses — callers
    must treat a ``None`` as "unknown" rather than "does not exist"
    because PyPI is a third party and a 503 does not mean the package
    vanished.
    """
    url = PYPI_JSON_URL.format(package=package)
    owned_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await http.get(url)
    except Exception as exc:  # noqa: BLE001 — network errors are WARN-worthy, not fatal
        logger.warning("PyPI fetch failed for %s: %s", package, exc)
        return None
    finally:
        if owned_client:
            await http.aclose()
    if resp.status_code == 404:
        # PyPI returns 404 for unknown packages. That's a concrete
        # "no such thing", distinct from a network hiccup.
        return {"__missing__": True}
    if resp.status_code >= 400:
        logger.warning("PyPI returned %s for %s", resp.status_code, package)
        return None
    try:
        parsed: dict[str, Any] = resp.json()
    except Exception as exc:  # noqa: BLE001 — malformed JSON is WARN-worthy
        logger.warning("PyPI JSON decode failed for %s: %s", package, exc)
        return None
    return parsed


def _parse_status_from_info(
    package: str,
    version: str,
    info: dict[str, Any] | None,
) -> PyPIVersionStatus:
    """Translate the raw PyPI JSON blob into a :class:`PyPIVersionStatus`.

    Split from the fetch so tests can feed pre-recorded fixtures and
    assert purely on the parsing logic without mocking HTTP.
    """
    if info is None:
        return PyPIVersionStatus(
            package=package,
            version=version,
            exists=False,
            yanked=False,
            reason="pypi unreachable",
        )
    if info.get("__missing__"):
        return PyPIVersionStatus(
            package=package,
            version=version,
            exists=False,
            yanked=False,
            reason=f"package '{package}' not on PyPI",
        )
    releases = info.get("releases") or {}
    # PyPI accepts both 'v1.2.3' and '1.2.3' style tags but the JSON
    # keys are canonical semver without the 'v' prefix. Normalise both
    # inputs for the comparison.
    canonical = version.removeprefix("v")
    artefacts = releases.get(canonical)
    if not artefacts:
        available = tuple(releases.keys())
        return PyPIVersionStatus(
            package=package,
            version=version,
            exists=False,
            yanked=False,
            reason=f"version {canonical} not published",
            available=available,
        )
    # A release is "yanked" when every artefact carries yanked=True.
    # Partial yanks (one wheel yanked, others live) still resolve, so
    # treat as installable.
    all_yanked = all(bool(a.get("yanked", False)) for a in artefacts)
    available = tuple(releases.keys())
    if all_yanked:
        return PyPIVersionStatus(
            package=package,
            version=version,
            exists=False,
            yanked=True,
            reason=f"version {canonical} yanked from PyPI",
            available=available,
        )
    return PyPIVersionStatus(
        package=package,
        version=version,
        exists=True,
        yanked=False,
        reason="installable",
        available=available,
    )


async def check_pypi_version(
    package: str,
    version: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 5.0,
) -> PyPIVersionStatus:
    """Resolve a single ``(package, version)`` pair against PyPI.

    Combines :func:`fetch_pypi_package_info` and
    :func:`_parse_status_from_info`. Prefer calling this from one-off
    doctor checks; use :func:`scan_fleet_version_drift` when checking
    a whole fleet because it reuses the http client.
    """
    info = await fetch_pypi_package_info(package, client=client, timeout=timeout)
    return _parse_status_from_info(package, version, info)


def _latest_non_yanked(info: dict[str, Any] | None) -> str | None:
    """Return the newest version in ``info.releases`` that is not yanked.

    PyPI does not guarantee sorted key order, so we sort by a naive
    semver tuple (major, minor, patch) with pre-release markers coming
    before their release. Good enough for shepherd's "what should I
    bump to" suggestion — operators still confirm in the PR.
    """
    if not info or info.get("__missing__"):
        return None
    releases = info.get("releases") or {}
    live: list[tuple[tuple[int, ...], str]] = []
    for ver, artefacts in releases.items():
        if not artefacts or all(bool(a.get("yanked", False)) for a in artefacts):
            continue
        parts = ver.split(".")
        try:
            key = tuple(int("".join(c for c in p if c.isdigit()) or "0") for p in parts)
        except ValueError:
            continue
        live.append((key, ver))
    if not live:
        return None
    live.sort()
    return live[-1][1]


async def scan_fleet_version_drift(
    pinned_versions: dict[str, str],
    *,
    package: str = DEFAULT_CARETAKER_PACKAGE,
    client: httpx.AsyncClient | None = None,
    timeout: float = 5.0,
) -> VersionDriftReport:
    """Scan a mapping of ``{repo: pinned_version}`` against PyPI.

    One PyPI round-trip per unique pinned version (shared metadata
    fetch). Returns a :class:`VersionDriftReport` whose
    :attr:`~VersionDriftReport.drifting_repos` lists the repos on
    yanked or missing versions — exactly the class of failure that
    space-tycoon PR #15 manifested as.
    """
    owned_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    report = VersionDriftReport()
    try:
        info = await fetch_pypi_package_info(package, client=http, timeout=timeout)
        report.recommended_version = _latest_non_yanked(info)
        # Fetch once per package; parse once per repo. Repos pinning
        # the same version share the same PyPIVersionStatus so we
        # avoid N re-parses across a dozen repos.
        cache: dict[str, PyPIVersionStatus] = {}
        for repo, version in pinned_versions.items():
            key = version.strip()
            if not key:
                report.errors.append(f"{repo}: empty version pin")
                continue
            if key not in cache:
                cache[key] = _parse_status_from_info(package, key, info)
            report.repos[repo] = cache[key]
    finally:
        if owned_client:
            await http.aclose()
    return report


__all__ = [
    "DEFAULT_CARETAKER_PACKAGE",
    "PYPI_JSON_URL",
    "PyPIVersionStatus",
    "VersionDriftReport",
    "check_pypi_version",
    "fetch_pypi_package_info",
    "scan_fleet_version_drift",
]
