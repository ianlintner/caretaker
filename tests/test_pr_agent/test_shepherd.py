"""Tests for the Shepherd PR cleanup loop.

Covers Delta B scope: scaffold + inventory/dedupe/promote phases.
Deltas C/D/E/F add handlers for mechanical fixers, rebase, reaper, merge
chain and LLM escalation. Those phases currently surface as placeholder
``skipped_phases`` entries so the report schema is stable from day one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caretaker.config import ShepherdConfig
from caretaker.github_client.models import MergeStateStatus, User
from caretaker.pr_agent.shepherd import ShepherdReport, run_shepherd
from tests.conftest import make_pr


class _FakeShepherdGH:
    """In-memory GitHubClient stub that records the shepherd's write calls.

    Only implements the surface shepherd Delta B touches:
      * ``list_pull_requests`` / ``enrich_merge_state_status`` — inventory
      * ``list_pull_request_files`` / ``add_issue_comment`` / ``update_issue``
        — downstream of close_duplicate_fix_prs
      * ``get_combined_status`` / ``mark_pull_request_ready`` — downstream of
        ready_valid_copilot_drafts
    """

    def __init__(
        self,
        open_prs: list[Any] | None = None,
        merge_states: dict[int, MergeStateStatus] | None = None,
        combined_statuses: dict[str, str] | None = None,
        update_branch_results: dict[int, bool] | None = None,
    ) -> None:
        self._open_prs = open_prs or []
        self._merge_states = merge_states or {}
        self._combined_statuses = combined_statuses or {}
        self._update_branch_results = update_branch_results or {}
        self.enrich_calls = 0
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.readied_node_ids: list[str] = []
        # Delta C surface
        self.update_branch_calls: list[tuple[int, str | None]] = []

    async def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[Any]:
        return list(self._open_prs)

    async def enrich_merge_state_status(self, owner: str, repo: str, prs: list[Any]) -> list[Any]:
        self.enrich_calls += 1
        for pr in prs:
            pr.merge_state_status = self._merge_states.get(pr.number)
        return prs

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, object]]:
        return []

    async def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def update_issue(self, owner: str, repo: str, number: int, **kw: object) -> None:
        if kw.get("state") == "closed":
            self.closed.append(number)

    async def get_combined_status(self, owner: str, repo: str, sha: str) -> str:
        return self._combined_statuses.get(sha, "pending")

    async def mark_pull_request_ready(self, node_id: str) -> bool:
        self.readied_node_ids.append(node_id)
        return True

    async def update_pull_request_branch(
        self,
        owner: str,
        repo: str,
        number: int,
        expected_head_sha: str | None = None,
    ) -> bool:
        self.update_branch_calls.append((number, expected_head_sha))
        # Default True so tests that don't care about branch update failures
        # still see a successful rebase recorded.
        return self._update_branch_results.get(number, True)

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[Any]:
        # Delta F surface — shepherd enriches PR context for stuck_pr_llm.
        # Empty is fine; stub doesn't need real check data for budget/filter tests.
        return []

    async def get_pr_reviews(self, owner: str, repo: str, number: int) -> list[Any]:
        # Delta F surface — see get_check_runs.
        return []


def test_shepherd_config_rejects_zero_stale_dirty_days() -> None:
    """`stale_dirty_days=0` would close every DIRTY draft on first run —
    reject at config load so operators see the error immediately."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShepherdConfig(stale_dirty_days=0)


def test_shepherd_config_rejects_negative_max_llm_calls() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShepherdConfig(max_llm_calls_per_run=-1)


@pytest.mark.asyncio
async def test_shepherd_disabled_returns_empty_report_without_listing_prs() -> None:
    """Disabled config is the byte-identical opt-out — no inventory call."""
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(enabled=False)

    report = await run_shepherd(gh, "o", "r", config)

    assert isinstance(report, ShepherdReport)
    assert report.inventoried == 0
    assert report.enriched == 0
    assert report.action_count == 0
    assert "shepherd:disabled" in report.skipped_phases
    # Never called list_pull_requests — we didn't touch the repo.
    assert gh.enrich_calls == 0


@pytest.mark.asyncio
async def test_shepherd_empty_repo_returns_zero_inventory() -> None:
    gh = _FakeShepherdGH(open_prs=[])
    config = ShepherdConfig(enabled=True)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.inventoried == 0
    assert report.enriched == 0
    assert report.action_count == 0
    # No PRs to enrich, but we DID list + short-circuit before enrichment.
    # The fake records enrichment on attribute access; list returning empty
    # short-circuits inventory before calling enrich, which is what we want.
    assert gh.enrich_calls == 0


@pytest.mark.asyncio
async def test_shepherd_inventory_enriches_merge_state_status() -> None:
    prs = [make_pr(number=10), make_pr(number=11), make_pr(number=12)]
    gh = _FakeShepherdGH(
        open_prs=prs,
        merge_states={
            10: MergeStateStatus.CLEAN,
            11: MergeStateStatus.BEHIND,
            # 12 intentionally omitted — enrichment failed for it, returns None.
        },
    )
    config = ShepherdConfig(enabled=True, dedupe=False, promote_drafts=False)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.inventoried == 3
    # Only PRs with a non-None merge_state_status count as enriched.
    assert report.enriched == 2
    assert gh.enrich_calls == 1


@pytest.mark.asyncio
async def test_shepherd_dedupe_closes_duplicate_cve_prs() -> None:
    """Phase 2 reuses close_duplicate_fix_prs — survivor = oldest."""
    older = make_pr(number=10, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    older.title = "Bump aiohttp for CVE-2026-34516"
    newer = make_pr(number=11, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    newer.title = "Fix CVE-2026-34516 in aiohttp"

    gh = _FakeShepherdGH(open_prs=[older, newer])
    config = ShepherdConfig(enabled=True, dedupe=True, promote_drafts=False)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.inventoried == 2
    assert report.closed_duplicate == [11]
    assert 11 in gh.closed
    # Survivor (older) stays open.
    assert 10 not in gh.closed


@pytest.mark.asyncio
async def test_shepherd_promote_flips_green_copilot_drafts() -> None:
    """Phase 3 reuses ready_valid_copilot_drafts — green drafts → ready."""
    copilot_user = User(login="copilot-swe-agent", id=1, type="Bot")
    draft = make_pr(number=42, draft=True, user=copilot_user)
    draft.head_sha = "sha-green"
    draft.node_id = "node-42"

    gh = _FakeShepherdGH(
        open_prs=[draft],
        combined_statuses={"sha-green": "success"},
    )
    config = ShepherdConfig(enabled=True, dedupe=False, promote_drafts=True)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.promoted == [42]
    assert gh.readied_node_ids == ["node-42"]


@pytest.mark.asyncio
async def test_shepherd_promote_skips_when_ci_not_green() -> None:
    copilot_user = User(login="copilot-swe-agent", id=1, type="Bot")
    draft = make_pr(number=43, draft=True, user=copilot_user)
    draft.head_sha = "sha-failing"
    draft.node_id = "node-43"

    gh = _FakeShepherdGH(
        open_prs=[draft],
        combined_statuses={"sha-failing": "failure"},
    )
    config = ShepherdConfig(enabled=True, dedupe=False, promote_drafts=True)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.promoted == []
    assert gh.readied_node_ids == []


@pytest.mark.asyncio
async def test_shepherd_promote_filters_out_just_closed_duplicates() -> None:
    """Dedupe then promote must not promote a PR we just closed."""
    # Both PRs are CVE dupes; both are Copilot drafts with green CI.
    copilot_user = User(login="copilot-swe-agent", id=1, type="Bot")
    older = make_pr(
        number=50,
        draft=True,
        user=copilot_user,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    older.title = "Fix CVE-2026-99999"
    older.head_sha = "sha-a"
    older.node_id = "node-50"

    newer = make_pr(
        number=51,
        draft=True,
        user=copilot_user,
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    newer.title = "Another fix for CVE-2026-99999"
    newer.head_sha = "sha-b"
    newer.node_id = "node-51"

    gh = _FakeShepherdGH(
        open_prs=[older, newer],
        combined_statuses={"sha-a": "success", "sha-b": "success"},
    )
    config = ShepherdConfig(enabled=True, dedupe=True, promote_drafts=True)

    report = await run_shepherd(gh, "o", "r", config)

    # The newer PR gets closed as duplicate; only the survivor (#50) is
    # promoted. Without the filter, ready_valid_copilot_drafts would also
    # try to promote #51, causing a spurious GraphQL call.
    assert report.closed_duplicate == [51]
    assert report.promoted == [50]
    assert gh.readied_node_ids == ["node-50"]


@pytest.mark.asyncio
async def test_shepherd_dry_run_override_wins_over_config() -> None:
    """Orchestrator-level dry_run overrides ShepherdConfig.dry_run."""
    older = make_pr(number=10, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    older.title = "Bump cryptography for CVE-2026-12345"
    newer = make_pr(number=11, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    newer.title = "Cryptography CVE-2026-12345"

    gh = _FakeShepherdGH(open_prs=[older, newer])
    config = ShepherdConfig(enabled=True, dry_run=False)

    report = await run_shepherd(gh, "o", "r", config, dry_run=True)

    # Dedupe still reports what it would close, but does not actually close.
    assert report.closed_duplicate == [11]
    assert gh.closed == []


@pytest.mark.asyncio
async def test_shepherd_records_phase_skip_placeholders_for_later_deltas() -> None:
    """Report schema is stable — unimplemented-delta phases announce themselves."""
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(enabled=True)

    report = await run_shepherd(gh, "o", "r", config)

    # Delta C landed auto_update_branch + stale_dirty_reaper as real phases.
    # Delta F landed llm_escalation — with no claude wired, it surfaces as
    # ``llm_escalation:no-claude`` rather than ``:pending-delta-f``.
    # Remaining placeholders for unimplemented deltas:
    assert "mechanical_fixes:pending-delta-c" in report.skipped_phases
    assert "merge_chain:pending-delta-c" in report.skipped_phases
    # And the now-real phases should NOT have pending tags.
    assert "auto_update_branch:pending-delta-c" not in report.skipped_phases
    assert "stale_dirty_reaper:pending-delta-c" not in report.skipped_phases
    assert "llm_escalation:pending-delta-f" not in report.skipped_phases
    # With claude=None (default), Delta F emits no-claude skip.
    assert "llm_escalation:no-claude" in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_llm_budget_zero_skips_escalation() -> None:
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(enabled=True, max_llm_calls_per_run=0)

    report = await run_shepherd(gh, "o", "r", config)

    assert "llm_escalation:budget-zero" in report.skipped_phases
    assert report.llm_budget_used == 0


@pytest.mark.asyncio
async def test_shepherd_disabled_phase_records_plain_skip() -> None:
    """Plain-skip entries distinguish 'operator turned it off' from 'pending'."""
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        mechanical_fixes=False,
        auto_update_branch=False,
        stale_dirty_reaper=False,
        merge_chain=False,
    )

    report = await run_shepherd(gh, "o", "r", config)

    assert "dedupe" in report.skipped_phases
    assert "promote_drafts" in report.skipped_phases
    assert "mechanical_fixes" in report.skipped_phases
    assert "auto_update_branch" in report.skipped_phases
    assert "stale_dirty_reaper" in report.skipped_phases
    assert "merge_chain" in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_inventory_failure_returns_error_without_raising() -> None:
    """One phase failing doesn't abort the run — errors are collected."""

    class _BrokenGH(_FakeShepherdGH):
        async def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list[Any]:
            raise RuntimeError("GitHub 502")

    gh = _BrokenGH()
    config = ShepherdConfig(enabled=True)

    report = await run_shepherd(gh, "o", "r", config)

    assert report.inventoried == 0
    assert any("inventory" in e for e in report.errors)
    # We bail out of the run after inventory failure — no phase placeholders
    # make sense when we never got a PR list.
    assert report.action_count == 0


@pytest.mark.asyncio
async def test_shepherd_update_issue_error_does_not_break_promote() -> None:
    """update_issue failing for a duplicate should not stop the promote phase."""

    class _UpdateBrokenGH(_FakeShepherdGH):
        async def update_issue(self, owner: str, repo: str, number: int, **kw: object) -> None:
            raise RuntimeError("update_issue API down")

    copilot_user = User(login="copilot-swe-agent", id=1, type="Bot")
    # Two CVE duplicates (so dedupe attempts to close #21 and raises in
    # update_issue), plus a clean Copilot draft that promote should still ready.
    dup_a = make_pr(number=20, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    dup_a.title = "Bump aiohttp CVE-2026-11111"
    dup_b = make_pr(number=21, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    dup_b.title = "aiohttp CVE-2026-11111 fix"
    draft = make_pr(number=30, draft=True, user=copilot_user)
    draft.head_sha = "sha-green"
    draft.node_id = "node-30"

    gh = _UpdateBrokenGH(
        open_prs=[dup_a, dup_b, draft],
        combined_statuses={"sha-green": "success"},
    )
    config = ShepherdConfig(enabled=True, dedupe=True, promote_drafts=True)

    report = await run_shepherd(gh, "o", "r", config)

    # close_duplicate_fix_prs swallows per-PR update_issue exceptions, so
    # no close lands. Promote must still complete.
    assert report.closed_duplicate == []
    assert report.promoted == [30]


# ──────────────────────── Delta C tests ────────────────────────


@pytest.mark.asyncio
async def test_shepherd_rebase_calls_update_branch_for_behind_prs() -> None:
    """Phase 5 — cascade handler: BEHIND PRs get ``update-branch``."""
    behind = make_pr(number=539)
    behind.head_sha = "sha-539"
    clean = make_pr(number=540)
    clean.head_sha = "sha-540"

    gh = _FakeShepherdGH(
        open_prs=[behind, clean],
        merge_states={
            539: MergeStateStatus.BEHIND,
            540: MergeStateStatus.CLEAN,
        },
    )
    config = ShepherdConfig(
        enabled=True, dedupe=False, promote_drafts=False, stale_dirty_reaper=False
    )

    report = await run_shepherd(gh, "o", "r", config)

    # Only the BEHIND PR got rebased; CLEAN PR was not touched.
    assert report.rebased == [539]
    assert gh.update_branch_calls == [(539, "sha-539")]


@pytest.mark.asyncio
async def test_shepherd_rebase_skips_when_api_returns_false() -> None:
    """If update-branch returns False (422/409), the PR is NOT counted."""
    behind = make_pr(number=539)
    behind.head_sha = "sha-539"

    gh = _FakeShepherdGH(
        open_prs=[behind],
        merge_states={539: MergeStateStatus.BEHIND},
        update_branch_results={539: False},
    )
    config = ShepherdConfig(
        enabled=True, dedupe=False, promote_drafts=False, stale_dirty_reaper=False
    )

    report = await run_shepherd(gh, "o", "r", config)

    # We attempted the call but the PR was NOT added to the rebased list.
    assert gh.update_branch_calls == [(539, "sha-539")]
    assert report.rebased == []


@pytest.mark.asyncio
async def test_shepherd_rebase_phase_off_records_skip_not_pending() -> None:
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(enabled=True, auto_update_branch=False)

    report = await run_shepherd(gh, "o", "r", config)

    assert "auto_update_branch" in report.skipped_phases
    # pending-delta-c marker must not appear when a delta is shipped.
    assert "auto_update_branch:pending-delta-c" not in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_rebase_honours_dry_run() -> None:
    """dry_run — PR is *counted* as would-be-rebased but API isn't called."""
    behind = make_pr(number=539)
    behind.head_sha = "sha-539"

    gh = _FakeShepherdGH(
        open_prs=[behind],
        merge_states={539: MergeStateStatus.BEHIND},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        stale_dirty_reaper=False,
        dry_run=True,
    )

    report = await run_shepherd(gh, "o", "r", config)

    assert report.rebased == [539]
    assert gh.update_branch_calls == []


@pytest.mark.asyncio
async def test_shepherd_reaper_closes_stale_dirty_draft() -> None:
    """Phase 6 — DIRTY draft older than threshold gets closed with a comment."""
    old_dirty = make_pr(number=528, draft=True)
    old_dirty.updated_at = (datetime.now(UTC) - timedelta(days=20)).isoformat()

    gh = _FakeShepherdGH(
        open_prs=[old_dirty],
        merge_states={528: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        auto_update_branch=False,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
    )

    report = await run_shepherd(gh, "o", "r", config)

    assert report.closed_stale == [528]
    assert 528 in gh.closed
    # Operator gets a trail: comment must explain why.
    assert len(gh.comments) == 1
    (number, body) = gh.comments[0]
    assert number == 528
    assert "Closing as stale" in body
    assert "caretaker:shepherd-stale-dirty" in body


@pytest.mark.asyncio
async def test_shepherd_reaper_ignores_fresh_dirty_draft() -> None:
    """A DIRTY draft just-created must NOT be reaped."""
    fresh_dirty = make_pr(number=528, draft=True)
    fresh_dirty.updated_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    gh = _FakeShepherdGH(
        open_prs=[fresh_dirty],
        merge_states={528: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        auto_update_branch=False,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
    )

    report = await run_shepherd(gh, "o", "r", config)

    assert report.closed_stale == []
    assert gh.closed == []
    assert gh.comments == []


@pytest.mark.asyncio
async def test_shepherd_reaper_ignores_non_draft_dirty() -> None:
    """Non-draft DIRTY PRs are NOT reaped — only drafts (StaleAgent handles ready PRs)."""
    old_dirty_ready = make_pr(number=99, draft=False)
    old_dirty_ready.updated_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    gh = _FakeShepherdGH(
        open_prs=[old_dirty_ready],
        merge_states={99: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        auto_update_branch=False,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
    )

    report = await run_shepherd(gh, "o", "r", config)

    assert report.closed_stale == []
    assert gh.closed == []


@pytest.mark.asyncio
async def test_shepherd_reaper_ignores_non_dirty_drafts() -> None:
    """A draft that is BEHIND (not DIRTY) must not be reaped — it's fixable."""
    old_behind = make_pr(number=77, draft=True)
    old_behind.updated_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    gh = _FakeShepherdGH(
        open_prs=[old_behind],
        merge_states={77: MergeStateStatus.BEHIND},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        auto_update_branch=False,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
    )

    report = await run_shepherd(gh, "o", "r", config)

    # BEHIND draft is not reaped; rebase phase is off so nothing happens.
    assert report.closed_stale == []
    assert gh.closed == []


@pytest.mark.asyncio
async def test_shepherd_reaper_honours_dry_run() -> None:
    old_dirty = make_pr(number=528, draft=True)
    old_dirty.updated_at = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    gh = _FakeShepherdGH(
        open_prs=[old_dirty],
        merge_states={528: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        dedupe=False,
        promote_drafts=False,
        auto_update_branch=False,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
        dry_run=True,
    )

    report = await run_shepherd(gh, "o", "r", config)

    # Would-be-closed appears in the report, but no GitHub write happened.
    assert report.closed_stale == [528]
    assert gh.closed == []
    assert gh.comments == []


@pytest.mark.asyncio
async def test_shepherd_reaper_phase_off_records_skip_not_pending() -> None:
    gh = _FakeShepherdGH(open_prs=[make_pr(number=1)])
    config = ShepherdConfig(enabled=True, stale_dirty_reaper=False)

    report = await run_shepherd(gh, "o", "r", config)

    assert "stale_dirty_reaper" in report.skipped_phases
    assert "stale_dirty_reaper:pending-delta-c" not in report.skipped_phases


# ---------------------------------------------------------------------------
# Delta F — LLM escalation with per-run budget guard
# ---------------------------------------------------------------------------


class _FakeClaude:
    """Sentinel truthy claude client. evaluate_stuck_pr_llm is monkeypatched
    in tests, so the actual client implementation is never invoked."""

    available = True


def _make_verdict(
    *,
    is_stuck: bool = True,
    stuck_reason: str = "ci_deadlock",
    recommended_action: str = "request_fix",
    confidence: float = 0.8,
    explanation: str = "Looks wedged on CI.",
) -> Any:
    """Build a StuckVerdict pydantic instance."""
    from caretaker.pr_agent.stuck_pr_llm import StuckVerdict

    return StuckVerdict(
        is_stuck=is_stuck,
        stuck_reason=stuck_reason,  # type: ignore[arg-type]
        recommended_action=recommended_action,  # type: ignore[arg-type]
        explanation=explanation,
        confidence=confidence,
    )


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_skipped_when_no_claude() -> None:
    """Without a claude client wired, the escalation phase is a no-op skip."""
    dirty = make_pr(number=200, draft=False)
    gh = _FakeShepherdGH(
        open_prs=[dirty],
        merge_states={200: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(enabled=True, max_llm_calls_per_run=3)

    report = await run_shepherd(gh, "o", "r", config, claude=None)

    assert "llm_escalation:no-claude" in report.skipped_phases
    assert report.llm_budget_used == 0
    assert report.llm_verdicts == []


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_honours_budget_and_marks_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget=2 with 3 candidates → only 2 LLM calls + budget-exhausted tag."""
    prs = [
        make_pr(number=301, draft=False),
        make_pr(number=302, draft=False),
        make_pr(number=303, draft=False),
    ]
    # Fresh timestamps so reaper doesn't touch them.
    now_iso = datetime.now(UTC).isoformat()
    for pr in prs:
        pr.updated_at = now_iso
        pr.created_at = now_iso
        pr.head_sha = f"sha-{pr.number}"
    gh = _FakeShepherdGH(
        open_prs=prs,
        merge_states={
            301: MergeStateStatus.DIRTY,
            302: MergeStateStatus.BLOCKED,
            303: MergeStateStatus.UNKNOWN,
        },
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=2,
        auto_update_branch=False,  # Avoid rebasing any BEHIND
        stale_dirty_reaper=False,
    )

    call_order: list[int] = []

    async def fake_eval(ctx: Any, *, claude: Any) -> Any:
        call_order.append(ctx.pr.number)
        return _make_verdict()

    monkeypatch.setattr("caretaker.pr_agent.shepherd.evaluate_stuck_pr_llm", fake_eval)

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    assert report.llm_budget_used == 2
    assert len(call_order) == 2
    assert len(report.llm_verdicts) == 2
    assert "llm_escalation:budget-exhausted" in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_skips_non_candidate_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLEAN / BEHIND / UNSTABLE / no-status PRs are NOT LLM candidates."""
    prs = [
        make_pr(number=401, draft=False),
        make_pr(number=402, draft=False),
        make_pr(number=403, draft=False),
        make_pr(number=404, draft=False),
        make_pr(number=405, draft=False),  # DIRTY — the only candidate
    ]
    now_iso = datetime.now(UTC).isoformat()
    for pr in prs:
        pr.updated_at = now_iso
        pr.created_at = now_iso
    gh = _FakeShepherdGH(
        open_prs=prs,
        merge_states={
            401: MergeStateStatus.CLEAN,
            402: MergeStateStatus.BEHIND,
            403: MergeStateStatus.UNSTABLE,
            # 404 intentionally left without a status → None
            405: MergeStateStatus.DIRTY,
        },
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=5,
        auto_update_branch=False,
        stale_dirty_reaper=False,
    )

    called_numbers: list[int] = []

    async def fake_eval(ctx: Any, *, claude: Any) -> Any:
        called_numbers.append(ctx.pr.number)
        return _make_verdict()

    monkeypatch.setattr("caretaker.pr_agent.shepherd.evaluate_stuck_pr_llm", fake_eval)

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    assert called_numbers == [405]
    assert report.llm_budget_used == 1
    assert "llm_escalation:budget-exhausted" not in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_skips_already_handled_prs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRs already rebased/reaped/promoted/deduped aren't re-analysed by LLM."""
    # Two DIRTY drafts: one gets reaped (old), one survives for LLM.
    old_iso = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    now_iso = datetime.now(UTC).isoformat()
    reaped = make_pr(number=501, draft=True)
    reaped.updated_at = old_iso
    survivor = make_pr(number=502, draft=False)
    survivor.updated_at = now_iso
    survivor.created_at = now_iso

    gh = _FakeShepherdGH(
        open_prs=[reaped, survivor],
        merge_states={
            501: MergeStateStatus.DIRTY,
            502: MergeStateStatus.DIRTY,
        },
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=5,
        stale_dirty_reaper=True,
        stale_dirty_days=14,
        auto_update_branch=False,
    )

    called_numbers: list[int] = []

    async def fake_eval(ctx: Any, *, claude: Any) -> Any:
        called_numbers.append(ctx.pr.number)
        return _make_verdict()

    monkeypatch.setattr("caretaker.pr_agent.shepherd.evaluate_stuck_pr_llm", fake_eval)

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    # Reaper closes 501; LLM only sees 502.
    assert 501 in report.closed_stale
    assert called_numbers == [502]
    assert report.llm_budget_used == 1


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_none_verdict_consumes_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_stuck_pr_llm returning None still counts against budget.

    Prevents retry-loops where a flaky LLM burns the whole run silently.
    """
    prs = [make_pr(number=n, draft=False) for n in (601, 602)]
    now_iso = datetime.now(UTC).isoformat()
    for pr in prs:
        pr.updated_at = now_iso
        pr.created_at = now_iso
    gh = _FakeShepherdGH(
        open_prs=prs,
        merge_states={
            601: MergeStateStatus.DIRTY,
            602: MergeStateStatus.BLOCKED,
        },
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=2,
        auto_update_branch=False,
        stale_dirty_reaper=False,
    )

    calls: list[int] = []

    async def fake_eval(ctx: Any, *, claude: Any) -> Any:
        calls.append(ctx.pr.number)
        return None  # Simulate StructuredCompleteError → None

    monkeypatch.setattr("caretaker.pr_agent.shepherd.evaluate_stuck_pr_llm", fake_eval)

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    # Both PRs tried, budget fully consumed, zero verdicts recorded.
    assert len(calls) == 2
    assert report.llm_budget_used == 2
    assert report.llm_verdicts == []
    # Budget matched candidate count exactly — not exhausted.
    assert "llm_escalation:budget-exhausted" not in report.skipped_phases


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_honours_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run → budget + verdicts recorded, but no LLM/GitHub calls."""
    pr = make_pr(number=701, draft=False)
    now_iso = datetime.now(UTC).isoformat()
    pr.updated_at = now_iso
    pr.created_at = now_iso
    gh = _FakeShepherdGH(
        open_prs=[pr],
        merge_states={701: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=2,
        dry_run=True,
        auto_update_branch=False,
        stale_dirty_reaper=False,
    )

    eval_called = False

    async def fake_eval(ctx: Any, *, claude: Any) -> Any:
        nonlocal eval_called
        eval_called = True
        return _make_verdict()

    monkeypatch.setattr("caretaker.pr_agent.shepherd.evaluate_stuck_pr_llm", fake_eval)

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    assert eval_called is False
    assert report.llm_budget_used == 1
    assert len(report.llm_verdicts) == 1
    assert report.llm_verdicts[0].get("dry_run") is True
    assert report.llm_verdicts[0].get("pr") == 701


@pytest.mark.asyncio
async def test_shepherd_llm_escalation_phase_off_records_plain_skip() -> None:
    """max_llm_calls_per_run=0 → plain budget-zero skip (unchanged by Delta F)."""
    dirty = make_pr(number=801, draft=False)
    dirty.updated_at = datetime.now(UTC).isoformat()
    dirty.created_at = datetime.now(UTC).isoformat()
    gh = _FakeShepherdGH(
        open_prs=[dirty],
        merge_states={801: MergeStateStatus.DIRTY},
    )
    config = ShepherdConfig(
        enabled=True,
        max_llm_calls_per_run=0,
        auto_update_branch=False,
        stale_dirty_reaper=False,
    )

    report = await run_shepherd(gh, "o", "r", config, claude=_FakeClaude())

    assert "llm_escalation:budget-zero" in report.skipped_phases
    assert "llm_escalation:no-claude" not in report.skipped_phases
    assert report.llm_budget_used == 0
