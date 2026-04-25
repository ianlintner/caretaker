"""Tests for the PR triage pass."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from caretaker.config import TriageConfig
from caretaker.github_client.models import CheckConclusion, CheckStatus, PRState, User
from caretaker.pr_agent.pr_triage import (
    close_binary_conflicted_prs,
    close_duplicate_fix_prs,
    close_empty_body_prs,
    close_empty_prs,
    ready_valid_copilot_drafts,
    run_pr_triage,
)
from tests.conftest import make_check_run, make_pr


class _FakeGH:
    def __init__(self, files_by_pr: dict[int, list[dict[str, object]]] | None = None) -> None:
        self.files_by_pr = files_by_pr or {}
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.statuses: dict[str, str] = {}
        self.check_runs_by_sha: dict[str, list[Any]] = {}
        self.readied_node_ids: list[str] = []

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

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[Any]:
        return self.check_runs_by_sha.get(ref, [])

    async def mark_pull_request_ready(self, node_id: str) -> bool:
        self.readied_node_ids.append(node_id)
        return True


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


# ── Fix 2: close_empty_body_prs ─────────────────────────────────────


@pytest.mark.asyncio
async def test_close_empty_body_prs_closes_blank_body() -> None:
    """A PR with no body at all should be closed."""
    pr = make_pr(number=99)
    # make_pr defaults body to "" — meets the empty criterion
    gh = _FakeGH()
    closed = await close_empty_body_prs(gh, "o", "r", [pr])
    assert closed == [99]
    assert 99 in gh.closed


@pytest.mark.asyncio
async def test_close_empty_body_prs_closes_boilerplate_body() -> None:
    """A PR body with only checklist boilerplate is treated as empty."""
    from tests.conftest import make_pr as _make_pr

    pr = _make_pr(number=100)
    pr = pr.model_copy(update={"body": "- [ ] TODO\n- [ ] N/A\n<!-- placeholder -->"})
    gh = _FakeGH()
    closed = await close_empty_body_prs(gh, "o", "r", [pr])
    assert closed == [100]


@pytest.mark.asyncio
async def test_close_empty_body_prs_keeps_real_description() -> None:
    """PRs with a substantive description must not be closed."""
    pr = make_pr(number=101)
    pr = pr.model_copy(
        update={"body": "This PR fixes the authentication bug by adding token refresh logic."}
    )
    gh = _FakeGH()
    closed = await close_empty_body_prs(gh, "o", "r", [pr])
    assert closed == []


@pytest.mark.asyncio
async def test_close_empty_body_prs_dry_run() -> None:
    pr = make_pr(number=102)
    gh = _FakeGH()
    closed = await close_empty_body_prs(gh, "o", "r", [pr], dry_run=True)
    assert closed == [102]
    assert gh.closed == []


@pytest.mark.asyncio
async def test_run_pr_triage_closes_empty_body_pr() -> None:
    """Scenario 10: empty-body PR is closed by the triage pass regardless of author."""
    pr = make_pr(number=20)
    # body="" — empty
    gh = _FakeGH(files_by_pr={20: [{"path": "src/real.py", "additions": 5}]})
    cfg = TriageConfig()
    report = await run_pr_triage(gh, "o", "r", [pr], cfg)
    assert 20 in report.closed_empty
    assert 20 in gh.closed


# ── ready_valid_copilot_drafts — Checks API gating ───────────────────


def _copilot_draft(number: int, sha: str, node_id: str = "") -> Any:
    """Return a draft Copilot PR stub."""
    pr = make_pr(number=number, draft=True, user=User(login="copilot-swe-agent", id=1, type="Bot"))
    pr.head_sha = sha
    pr.node_id = node_id or f"node-{number}"
    return pr


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_all_success() -> None:
    """All checks passing → PR is marked ready."""
    pr = _copilot_draft(10, "sha-green", "node-10")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-green"] = [
        make_check_run("lint", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == [10]
    assert gh.readied_node_ids == ["node-10"]


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_ignores_self_check() -> None:
    """caretaker/pr-readiness in_progress must not block promotion (self-gating guard)."""
    pr = _copilot_draft(11, "sha-self-gate", "node-11")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-self-gate"] = [
        make_check_run("lint", CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
        make_check_run("caretaker/pr-readiness", CheckStatus.IN_PROGRESS, None),
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == [11]
    assert gh.readied_node_ids == ["node-11"]


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_blocks_on_failure() -> None:
    """A failing check prevents promotion."""
    pr = _copilot_draft(12, "sha-fail", "node-12")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-fail"] = [
        make_check_run("lint", CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
        make_check_run("tests", CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == []
    assert gh.readied_node_ids == []


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_blocks_on_in_progress() -> None:
    """A non-ignored in-progress check keeps the PR in draft."""
    pr = _copilot_draft(13, "sha-pending", "node-13")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-pending"] = [
        make_check_run("lint", CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
        make_check_run("tests", CheckStatus.IN_PROGRESS, None),
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == []
    assert gh.readied_node_ids == []


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_blocks_on_empty_checks() -> None:
    """No check runs at all → PENDING → PR stays in draft."""
    pr = _copilot_draft(14, "sha-empty", "node-14")
    gh = _FakeGH()
    # check_runs_by_sha has no entry for "sha-empty" → returns []

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == []
    assert gh.readied_node_ids == []


@pytest.mark.asyncio
async def test_ready_valid_copilot_drafts_dry_run() -> None:
    """dry_run=True returns the would-be list but does not call mark_pull_request_ready."""
    pr = _copilot_draft(15, "sha-dry", "node-15")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-dry"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr], dry_run=True)

    assert readied == [15]
    assert gh.readied_node_ids == []
