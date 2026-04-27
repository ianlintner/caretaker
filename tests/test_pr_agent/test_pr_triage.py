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
        self.review_requests: list[tuple[int, list[str]]] = []

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

    async def request_reviewers(
        self, owner: str, repo: str, pr_number: int, reviewers: list[str]
    ) -> dict[str, Any]:
        self.review_requests.append((pr_number, reviewers))
        return {}


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
async def test_close_duplicate_fix_prs_package_bump_prefers_higher_target() -> None:
    """For pkg:* groups (upgrade PRs), the survivor is the one targeting the
    higher version, not the older PR. F-6 regression — pre-fix this would have
    closed #21 (target 9.0.3) in favor of #20 (target 9.0.2), stalling the
    upgrade chain."""
    a = make_pr(number=20, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    a.title = "Bump pytest from 8 to 9.0.2"
    b = make_pr(number=21, created_at=datetime(2026, 1, 2, tzinfo=UTC))
    b.title = "Bump pytest from 8 to 9.0.3"
    gh = _FakeGH()
    closed = await close_duplicate_fix_prs(gh, "o", "r", [a, b])
    assert closed == [20]
    # Close comment references the survivor (the higher-target PR).
    assert any("#21" in body for (_, body) in gh.comments)


@pytest.mark.asyncio
async def test_close_duplicate_fix_prs_upgrade_chain_f6_regression() -> None:
    """F-6 from the 2026-04-27 QA cycle: a stale 2-day-old upgrade PR
    targeting v0.19.4 silently closed every newer v0.25.0 bump caretaker
    auto-opened. Pre-fix, oldest-wins-by-created_at made the stale PR the
    survivor; post-fix, the highest-target survives even when older."""
    stale = make_pr(number=39, created_at=datetime(2026, 4, 25, 22, tzinfo=UTC))
    stale.title = "chore: upgrade caretaker pin from v0.19.3 to v0.19.4"
    fresh = make_pr(number=70, created_at=datetime(2026, 4, 27, 15, tzinfo=UTC))
    fresh.title = "chore: bump caretaker pin from v0.24.0 to v0.25.0"
    gh = _FakeGH()
    closed = await close_duplicate_fix_prs(gh, "o", "r", [stale, fresh])
    # The stale v0.19.4 PR is closed; the fresh v0.25.0 PR survives.
    assert closed == [39]
    assert any("#70" in body for (_, body) in gh.comments)


@pytest.mark.asyncio
async def test_close_duplicate_fix_prs_pkg_dedup_is_input_order_stable() -> None:
    """Reversing input order must not change the survivor for pkg:* groups
    (same property as the CVE oldest-wins variant — different selector,
    same invariant)."""
    older = make_pr(number=100, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    older.title = "chore: upgrade caretaker pin from v1.0.0 to v1.1.0"
    middle = make_pr(number=101, created_at=datetime(2026, 1, 5, tzinfo=UTC))
    middle.title = "chore: upgrade caretaker pin from v1.1.0 to v1.2.0"
    newest = make_pr(number=102, created_at=datetime(2026, 1, 10, tzinfo=UTC))
    newest.title = "chore: upgrade caretaker pin from v1.2.0 to v1.3.0"

    for ordering in ([older, middle, newest], [newest, older, middle]):
        gh = _FakeGH()
        closed = await close_duplicate_fix_prs(gh, "o", "r", ordering)
        # #102 has the highest target (v1.3.0) and survives in every input order.
        assert sorted(closed) == [100, 101]
        assert all("#102" in body for (_, body) in gh.comments)


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


# ── ready_valid_copilot_drafts — caretaker branch promotion ──────────


def _caretaker_draft(number: int, sha: str, node_id: str = "", branch: str = "") -> Any:
    """Return a draft caretaker PR stub (claude/ branch, human author)."""
    from tests.conftest import make_pr as _make_pr

    pr = _make_pr(number=number, draft=True)
    pr.head_sha = sha
    pr.head_ref = branch or f"claude/fix-something-{number}"
    pr.node_id = node_id or f"node-caretaker-{number}"
    return pr


@pytest.mark.asyncio
async def test_ready_caretaker_draft_when_ci_green() -> None:
    """Caretaker draft on claude/ branch is promoted when CI passes."""
    pr = _caretaker_draft(20, "sha-ct-green", "node-ct-20")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-ct-green"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == [20]
    assert gh.readied_node_ids == ["node-ct-20"]


@pytest.mark.asyncio
async def test_ready_caretaker_draft_requests_copilot_review() -> None:
    """After promoting a caretaker draft, Copilot review is requested."""
    pr = _caretaker_draft(21, "sha-ct-review", "node-ct-21")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-ct-review"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert gh.review_requests == [(21, ["copilot-pull-request-reviewer"])]


@pytest.mark.asyncio
async def test_copilot_draft_does_not_request_review() -> None:
    """Promoting a Copilot-authored draft does NOT trigger a review request."""
    pr = _copilot_draft(22, "sha-cp-noreview", "node-cp-22")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-cp-noreview"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert gh.readied_node_ids == ["node-cp-22"]
    assert gh.review_requests == []


@pytest.mark.asyncio
async def test_ready_caretaker_draft_dry_run_no_review_request() -> None:
    """dry_run suppresses both the ready flip and the review request."""
    pr = _caretaker_draft(23, "sha-ct-dry", "node-ct-23")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-ct-dry"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr], dry_run=True)

    assert readied == [23]
    assert gh.readied_node_ids == []
    assert gh.review_requests == []


@pytest.mark.asyncio
async def test_caretaker_branch_prefix_caretaker_slash() -> None:
    """caretaker/ branch prefix is also recognised as a caretaker PR."""
    pr = _caretaker_draft(24, "sha-ct-prefix", "node-ct-24", branch="caretaker/some-fix")
    gh = _FakeGH()
    gh.check_runs_by_sha["sha-ct-prefix"] = [
        make_check_run("ci", CheckStatus.COMPLETED, CheckConclusion.SUCCESS)
    ]

    readied = await ready_valid_copilot_drafts(gh, "o", "r", [pr])

    assert readied == [24]
    assert gh.review_requests == [(24, ["copilot-pull-request-reviewer"])]
