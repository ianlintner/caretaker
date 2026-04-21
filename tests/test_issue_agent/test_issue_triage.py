"""Tests for the issue triage pass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from caretaker.config import TriageConfig
from caretaker.github_client.models import Issue, Label, User
from caretaker.issue_agent.issue_triage import (
    close_duplicate_issues,
    close_empty_issues,
    close_resolved_issues,
    mark_stale_issues,
    run_issue_triage,
)
from caretaker.state.models import TrackedIssue
from tests.conftest import make_pr


def make_issue(
    number: int,
    title: str = "issue",
    body: str = "this is a valid issue body with enough substance",
    labels: list[Label] | None = None,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Issue:
    user = User(login="dev", id=1, type="User")
    created = created_at or datetime(2026, 1, 1, tzinfo=UTC)
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        user=user,
        labels=labels or [],
        created_at=created,
        updated_at=updated_at or created,
    )


class _FakeGH:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.closing_refs: dict[int, list[int]] = {}

    async def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def update_issue(self, owner: str, repo: str, number: int, **kw: object) -> None:
        if kw.get("state") == "closed":
            self.closed.append(number)

    async def get_closing_issue_numbers(self, owner: str, repo: str, number: int) -> list[int]:
        return self.closing_refs.get(number, [])


@pytest.mark.asyncio
async def test_close_empty_issues() -> None:
    empty = make_issue(1, body="")
    stub = make_issue(2, body="   ")
    real = make_issue(3, body="Detailed bug report with repro steps and stack trace.")
    gh = _FakeGH()
    closed = await close_empty_issues(gh, "o", "r", [empty, stub, real])
    assert set(closed) == {1, 2}


@pytest.mark.asyncio
async def test_close_empty_issues_respects_keep_open_label() -> None:
    empty = make_issue(1, body="", labels=[Label(name="keep-open")])
    gh = _FakeGH()
    closed = await close_empty_issues(gh, "o", "r", [empty])
    assert closed == []


@pytest.mark.asyncio
async def test_close_duplicate_issues_by_cve() -> None:
    older = make_issue(
        10,
        title="CVE-2026-34516 in aiohttp",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = make_issue(
        11,
        title="CVE-2026-34516 found by scanner",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    gh = _FakeGH()
    closed = await close_duplicate_issues(gh, "o", "r", [older, newer])
    # Older is kept — newer closed as duplicate.
    assert closed == [11]


@pytest.mark.asyncio
async def test_mark_stale_issues_closes_old() -> None:
    fresh = make_issue(1, updated_at=datetime.now(UTC) - timedelta(days=5))
    stale = make_issue(2, updated_at=datetime.now(UTC) - timedelta(days=60))
    gh = _FakeGH()
    closed = await mark_stale_issues(gh, "o", "r", [fresh, stale], stale_days=30)
    assert closed == [2]


@pytest.mark.asyncio
async def test_mark_stale_issues_zero_disables() -> None:
    old = make_issue(1, updated_at=datetime.now(UTC) - timedelta(days=300))
    gh = _FakeGH()
    closed = await mark_stale_issues(gh, "o", "r", [old], stale_days=0)
    assert closed == []


@pytest.mark.asyncio
async def test_close_resolved_issues_via_closing_refs() -> None:
    issue = make_issue(42)
    merged_pr = make_pr(number=100, merged=True)
    gh = _FakeGH()
    gh.closing_refs[100] = [42]
    closed = await close_resolved_issues(gh, "o", "r", [issue], [merged_pr], {})
    assert closed == [42]


@pytest.mark.asyncio
async def test_close_resolved_issues_via_tracked_assignment() -> None:
    issue = make_issue(42)
    merged_pr = make_pr(number=100, merged=True)
    gh = _FakeGH()
    tracked = {42: TrackedIssue(number=42, assigned_pr=100)}
    closed = await close_resolved_issues(gh, "o", "r", [issue], [merged_pr], tracked)
    assert closed == [42]


@pytest.mark.asyncio
async def test_run_issue_triage_disabled() -> None:
    gh = _FakeGH()
    cfg = TriageConfig(enabled=False)
    report = await run_issue_triage(gh, "o", "r", [], [], {}, cfg)
    assert report.closed_empty == []


@pytest.mark.asyncio
async def test_run_issue_triage_issue_triage_toggle() -> None:
    empty = make_issue(1, body="")
    gh = _FakeGH()
    cfg = TriageConfig(issue_triage=False)
    report = await run_issue_triage(gh, "o", "r", [empty], [], {}, cfg)
    assert report.closed_empty == []
    assert gh.closed == []
