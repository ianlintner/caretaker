"""Tests for the PyPI version-drift scanner used by shepherd + doctor.

These tests never hit the real network. ``httpx.MockTransport`` wraps a
stubbed handler that returns hand-crafted PyPI JSON payloads; this keeps
assertions deterministic and the suite offline.

The scenarios mirror the space-tycoon PR #15 root cause class:

* Yanked version (the actual #15 failure).
* Version never published (typo in pin, or bumped beyond current).
* Package rename (``caretaker`` → ``caretaker-github``).
* PyPI transport failure (503 / DNS).
* Live version (happy path).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from caretaker.fleet.version_drift import (
    DEFAULT_CARETAKER_PACKAGE,
    PYPI_JSON_URL,
    _latest_non_yanked,
    _parse_status_from_info,
    check_pypi_version,
    fetch_pypi_package_info,
    scan_fleet_version_drift,
)


def _pypi_info(
    *,
    releases: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Shape a fake PyPI JSON payload matching the real schema.

    We only populate the keys the scanner reads (``releases`` with
    per-artefact ``yanked`` flags); the real payload has dozens of
    other fields we don't care about.
    """
    return {"info": {"name": DEFAULT_CARETAKER_PACKAGE}, "releases": releases}


def _make_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Build a ``MockTransport`` that returns responses by request URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in responses:
            return responses[url]
        return httpx.Response(status_code=404, json={"message": "not found"})

    return httpx.MockTransport(handler)


# ── Parse-only tests (no HTTP) ────────────────────────────────────────


def test_parse_status_none_info_is_unreachable() -> None:
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "0.19.2", None)
    assert status.exists is False
    assert status.yanked is False
    assert "unreachable" in status.reason


def test_parse_status_missing_package_is_not_exists() -> None:
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "0.19.2", {"__missing__": True})
    assert status.exists is False
    assert status.yanked is False
    assert "not on PyPI" in status.reason


def test_parse_status_unknown_version_is_not_exists() -> None:
    info = _pypi_info(releases={"0.19.2": [{"yanked": False}]})
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "0.7.2", info)
    assert status.exists is False
    assert status.yanked is False
    assert "not published" in status.reason
    assert "0.19.2" in status.available


def test_parse_status_yanked_everywhere_is_yanked() -> None:
    # This is the exact shape that bit space-tycoon PR #15: version
    # present but every wheel/sdist flagged yanked.
    info = _pypi_info(
        releases={
            "0.7.2": [{"yanked": True}, {"yanked": True}],
            "0.19.2": [{"yanked": False}],
        }
    )
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "0.7.2", info)
    assert status.exists is False
    assert status.yanked is True
    assert "yanked" in status.reason


def test_parse_status_partial_yank_still_installable() -> None:
    # pip can still install from a partially-yanked release, so we
    # must not flag these as broken.
    info = _pypi_info(releases={"0.19.2": [{"yanked": True}, {"yanked": False}]})
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "0.19.2", info)
    assert status.exists is True
    assert status.yanked is False


def test_parse_status_with_v_prefix_normalised() -> None:
    info = _pypi_info(releases={"0.19.2": [{"yanked": False}]})
    status = _parse_status_from_info(DEFAULT_CARETAKER_PACKAGE, "v0.19.2", info)
    assert status.exists is True


def test_latest_non_yanked_picks_newest_live_version() -> None:
    info = _pypi_info(
        releases={
            "0.7.2": [{"yanked": True}],
            "0.19.1": [{"yanked": False}],
            "0.19.2": [{"yanked": False}],
            "0.10.0": [{"yanked": False}],
        }
    )
    assert _latest_non_yanked(info) == "0.19.2"


def test_latest_non_yanked_ignores_all_yanked() -> None:
    info = _pypi_info(releases={"0.7.2": [{"yanked": True}]})
    assert _latest_non_yanked(info) is None


def test_latest_non_yanked_none_input() -> None:
    assert _latest_non_yanked(None) is None
    assert _latest_non_yanked({"__missing__": True}) is None


# ── fetch_pypi_package_info (HTTP, mocked) ────────────────────────────


@pytest.mark.asyncio
async def test_fetch_pypi_package_info_success() -> None:
    payload = _pypi_info(releases={"0.19.2": [{"yanked": False}]})
    url = PYPI_JSON_URL.format(package=DEFAULT_CARETAKER_PACKAGE)
    transport = _make_transport(
        {url: httpx.Response(status_code=200, content=json.dumps(payload))}
    )
    async with httpx.AsyncClient(transport=transport) as client:
        info = await fetch_pypi_package_info(DEFAULT_CARETAKER_PACKAGE, client=client)
    assert info is not None
    assert "0.19.2" in info["releases"]


@pytest.mark.asyncio
async def test_fetch_pypi_package_info_404_returns_missing_sentinel() -> None:
    url = PYPI_JSON_URL.format(package="nonsense-pkg")
    transport = _make_transport({url: httpx.Response(status_code=404, json={"m": "x"})})
    async with httpx.AsyncClient(transport=transport) as client:
        info = await fetch_pypi_package_info("nonsense-pkg", client=client)
    assert info == {"__missing__": True}


@pytest.mark.asyncio
async def test_fetch_pypi_package_info_503_returns_none() -> None:
    # 503 is "try again later" from PyPI — we must distinguish it
    # from a real 404 or the scanner would falsely flag packages as
    # missing during PyPI outages.
    url = PYPI_JSON_URL.format(package=DEFAULT_CARETAKER_PACKAGE)
    transport = _make_transport(
        {url: httpx.Response(status_code=503, json={"error": "upstream"})}
    )
    async with httpx.AsyncClient(transport=transport) as client:
        info = await fetch_pypi_package_info(DEFAULT_CARETAKER_PACKAGE, client=client)
    assert info is None


@pytest.mark.asyncio
async def test_fetch_pypi_package_info_transport_error_returns_none() -> None:
    def raising(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(raising)) as client:
        info = await fetch_pypi_package_info(DEFAULT_CARETAKER_PACKAGE, client=client)
    assert info is None


# ── check_pypi_version (integrates fetch + parse) ─────────────────────


@pytest.mark.asyncio
async def test_check_pypi_version_yanked_flows_through() -> None:
    payload = _pypi_info(
        releases={
            "0.7.2": [{"yanked": True}],
            "0.19.2": [{"yanked": False}],
        }
    )
    url = PYPI_JSON_URL.format(package=DEFAULT_CARETAKER_PACKAGE)
    transport = _make_transport(
        {url: httpx.Response(status_code=200, content=json.dumps(payload))}
    )
    async with httpx.AsyncClient(transport=transport) as client:
        status = await check_pypi_version(DEFAULT_CARETAKER_PACKAGE, "0.7.2", client=client)
    assert status.yanked is True
    assert status.exists is False


# ── scan_fleet_version_drift (end-to-end) ─────────────────────────────


@pytest.mark.asyncio
async def test_scan_fleet_version_drift_identifies_drifting_repos() -> None:
    # Two repos pin 0.7.2 (yanked), one pins 0.19.2 (live), one pins
    # nothing (empty string → error list entry, not a false-positive
    # drift).
    payload = _pypi_info(
        releases={
            "0.7.2": [{"yanked": True}],
            "0.19.1": [{"yanked": False}],
            "0.19.2": [{"yanked": False}],
        }
    )
    url = PYPI_JSON_URL.format(package=DEFAULT_CARETAKER_PACKAGE)
    transport = _make_transport(
        {url: httpx.Response(status_code=200, content=json.dumps(payload))}
    )
    pinned = {
        "ianlintner/space-tycoon": "0.7.2",
        "ianlintner/other-yanked": "0.7.2",
        "ianlintner/current": "0.19.2",
        "ianlintner/empty-pin": "",
    }
    async with httpx.AsyncClient(transport=transport) as client:
        report = await scan_fleet_version_drift(pinned, client=client)
    assert report.recommended_version == "0.19.2"
    assert sorted(report.drifting_repos) == [
        "ianlintner/other-yanked",
        "ianlintner/space-tycoon",
    ]
    assert report.repos["ianlintner/current"].exists is True
    assert any("empty-pin" in err for err in report.errors)


@pytest.mark.asyncio
async def test_scan_fleet_version_drift_pypi_unreachable_surfaces_every_repo_as_drift() -> None:
    # When PyPI itself is unreachable we intentionally mark every
    # repo's status as "unreachable" rather than silently report no
    # drift — shepherd's auto-bump logic must not treat an outage as
    # a green light.
    def raising(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail")

    pinned = {"ianlintner/repo-a": "0.19.2", "ianlintner/repo-b": "0.19.1"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(raising)) as client:
        report = await scan_fleet_version_drift(pinned, client=client)
    assert report.recommended_version is None
    assert all(status.reason == "pypi unreachable" for status in report.repos.values())
    # All repos land in drifting_repos because exists=False on unreachable.
    assert set(report.drifting_repos) == set(pinned.keys())


@pytest.mark.asyncio
async def test_scan_fleet_version_drift_deduplicates_shared_versions() -> None:
    # Eight repos, two unique pins → the scanner should only parse
    # PyPI metadata twice and share the PyPIVersionStatus across
    # same-version repos. We assert the shared-object identity so a
    # future refactor that accidentally re-parses per-repo doesn't
    # sneak past review.
    payload = _pypi_info(releases={"0.19.2": [{"yanked": False}]})
    url = PYPI_JSON_URL.format(package=DEFAULT_CARETAKER_PACKAGE)
    transport = _make_transport(
        {url: httpx.Response(status_code=200, content=json.dumps(payload))}
    )
    pinned = {f"ianlintner/repo-{i}": "0.19.2" for i in range(8)}
    async with httpx.AsyncClient(transport=transport) as client:
        report = await scan_fleet_version_drift(pinned, client=client)
    statuses = [report.repos[key] for key in pinned]
    # All repos must share the *same* PyPIVersionStatus object.
    assert all(s is statuses[0] for s in statuses)
