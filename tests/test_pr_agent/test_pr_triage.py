"""Tests for the PR triage pass."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.config import TriageConfig
from caretaker.github_client.models import PRState
from caretaker.pr_agent.pr_triage import (
    close_binary_conflicted_prs,
    close_duplicate_fix_prs,
    close_empty_prs,
    run_pr_triage,
)
from tests.conftest import make_pr


class _FakeGH:
    def __init__(self, files_by_pr: dict[int, list[dict[str, object]]] | None = None) -> None:
        self.files_by_pr = files_by_pr or {}
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.statuses: dict[str, str] = {}

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        return self.files_by_pr.get(number, [])

    async def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def update_issue(self, owner: str, repo: str, number: int, **kw: object) -> None:
        if kw.get("state") == "closed":
            self.closed.append(number)

    async def get_combined_status(self, owner: str, repo: str, sha: str) -> str:
        return self.statuses.get(sha, "pending")


@pytest.mark.asyncio
async def test_close_empty_prs_binary_only_diff() -> None:
    pr = make_pr(number=1)
    files = [{"path": ".caretaker-memory.db", "additions": 0, "deletions": 0}]
    gh = _FakeGH(files_by_pr={1: files})
    closed = await close_empty_prs(gh, "o", "r", [pr], [".caretaker-memory.db"])
    assert closed == [1]
    assert 1 in gh.closed


@pytest.mark.asyncio
async def test_close_empty_prs_skips_real_diffs() -> None:
    pr = make_pr(number=2)
    gh = _FakeGH(files_by_pr={2: [{"path": "src/foo.py", "additions": 5, "deletions": 1}]})
    closed = await close_empty_prs(gh, "o", "r", [pr], [".caretaker-memory.db"])
    assert closed == []


@pytest.mark.asyncio
async def test_close_empty_prs_dry_run_does_not_close() -> None:
    pr = make_pr(number=3)
    gh = _FakeGH(files_by_pr={3: [{"path": ".caretaker-memory.db"}]})
    closed = await close_empty_prs(gh, "o", "r", [pr], [".caretaker-memory.db"], dry_run=True)
    assert closed == [3]
    assert gh.closed == []


@pytest.mark.asyncio
async def test_close_duplicate_fix_prs_by_cve() -> None:
    # Survivor policy: oldest wins (canonical review history lives on the
    # first PR). Mirrors close_duplicate_issues in issue_agent/issue_triage.py.
    older = make_pr(number=10, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    older.title = "Bump aiohttp for CVE-2026-34516"
    newer = make_pr(number=11, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    newer.title = "Fix CVE-2026-34516 in aiohttp (proper pin)"
    gh = _FakeGH()
    closed = await close_duplicate_fix_prs(gh, "o", "r", [older, newer])
    assert closed == [11]
    # Close comment references the survivor (the older PR).
    assert any("#10" in body for (_, body) in gh.comments)


@pytest.mark.asyncio
async def test_close_duplicate_fix_prs_package_bump() -> None:
    # Survivor policy: oldest wins.
    a = make_pr(number=20, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    a.title = "Bump pytest from 8 to 9.0.2"
    b = make_pr(number=21, created_at=datetime(2026, 1, 2, tzinfo=UTC))
    b.title = "Bump pytest from 8 to 9.0.3"
    gh = _FakeGH()
    closed = await close_duplicate_fix_prs(gh, "o", "r", [a, b])
    assert closed == [21]


@pytest.mark.asyncio
async def test_close_duplicate_fix_prs_survivor_matches_issue_triage_policy() -> None:
    """Regression for T-S4: pr_triage survivor must match issue_triage (oldest wins).

    Reversing the list order must not change the survivor — this catches any
    regression to "newest wins" that would be hidden by input ordering.
    """
    first = make_pr(number=50, created_at=datetime(2026, 3, 1, tzinfo=UTC))
    first.title = "Bump requests for CVE-2026-99999"
    second = make_pr(number=51, created_at=datetime(2026, 3, 10, tzinfo=UTC))
    second.title = "CVE-2026-99999 requests pin"
    third = make_pr(number=52, created_at=datetime(2026, 3, 15, tzinfo=UTC))
    third.title = "Fix CVE-2026-99999 in requests"

    gh = _FakeGH()
    closed = await close_duplicate_fix_prs(gh, "o", "r", [third, first, second])
    # Oldest (#50) wins regardless of input order; #51 and #52 close.
    assert sorted(closed) == [51, 52]
    # All close comments reference #50 as the superseding PR.
    assert all("#50" in body for (_, body) in gh.comments)


@pytest.mark.asyncio
async def test_close_binary_conflicted_prs() -> None:
    pr = make_pr(number=30, mergeable=False)
    gh = _FakeGH(
        files_by_pr={
            30: [
                {"path": ".caretaker-memory.db"},
                {"path": "src/foo.py"},
            ]
        }
    )
    closed = await close_binary_conflicted_prs(gh, "o", "r", [pr], [".caretaker-memory.db"])
    assert closed == [30]


@pytest.mark.asyncio
async def test_close_binary_conflicted_prs_skips_real_conflicts() -> None:
    # Conflict involves real source files — leave it for humans.
    pr = make_pr(number=31, mergeable=False)
    gh = _FakeGH(files_by_pr={31: [{"path": "src/a.py"}, {"path": "src/b.py"}]})
    closed = await close_binary_conflicted_prs(gh, "o", "r", [pr], [".caretaker-memory.db"])
    assert closed == []


@pytest.mark.asyncio
async def test_run_pr_triage_disabled_returns_empty() -> None:
    gh = _FakeGH()
    cfg = TriageConfig(enabled=False)
    report = await run_pr_triage(gh, "o", "r", [], cfg)
    assert report.closed_empty == []
    assert report.closed_duplicate == []


@pytest.mark.asyncio
async def test_run_pr_triage_pr_triage_toggle() -> None:
    pr = make_pr(number=40, state=PRState.OPEN)
    gh = _FakeGH(files_by_pr={40: [{"path": ".caretaker-memory.db"}]})
    cfg = TriageConfig(pr_triage=False)
    report = await run_pr_triage(gh, "o", "r", [pr], cfg)
    assert report.closed_empty == []
    assert gh.closed == []
