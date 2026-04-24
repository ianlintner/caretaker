"""Shepherd mode — codifies the manual PR cleanup loop.

Background
----------
On 2026-04-24 a human operator walked a fleet of 7 stuck caretaker PRs and
1 space-tycoon PR through the following motions:

1. Inventory open PRs + enrich with GraphQL ``mergeStateStatus``.
2. Close duplicate fix PRs (survivor = oldest).
3. Flip green Copilot drafts ready-for-review.
4. Apply mechanical lint fixers (ruff --fix, line-wrap) before reaching for
   an LLM.
5. Rebase ``BEHIND`` PRs via ``PUT /pulls/{n}/update-branch``.
6. Reap ``DIRTY`` drafts older than N days as stale.
7. Merge clean PRs one at a time, re-rebasing siblings between each merge.
8. Only after all of the above, escalate truly stuck PRs to the stuck_pr_llm.

Most of these primitives already exist inside :mod:`caretaker.pr_agent` and
:mod:`caretaker.self_heal_agent`; this module is the *sequencing* layer so the
loop runs as a routine instead of requiring a human.

Delta B shipped phases 1–3 (inventory, dedupe, promote). Delta C added
phases 5–6 (rebase BEHIND, reap DIRTY drafts). Delta D added PyPI
existence checks in ``caretaker.fleet.version_drift``. Delta E wired the
``shepherd`` mode, workflow enum, and agent registration. Delta F (this
change) lands phase 8: budgeted LLM escalation for PRs still stuck after
every mechanical phase. Each delta is guarded by its own
``ShepherdConfig`` knob, so operators can stage the rollout.

Mechanical fixers (phase 4) and the merge chain (phase 7) remain as
placeholder skip tags — those still need dedicated deltas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.github_client.models import MergeStateStatus
from caretaker.pr_agent.pr_triage import (
    close_duplicate_fix_prs,
    ready_valid_copilot_drafts,
)
from caretaker.pr_agent.stuck_pr_llm import PRStuckContext, evaluate_stuck_pr_llm

if TYPE_CHECKING:
    from caretaker.config import ShepherdConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)

_STALE_DIRTY_COMMENT = (
    "Closing as stale — this PR has been in a DIRTY (merge conflict) draft "
    "state for {days} days. Caretaker's shepherd routine reaps DIRTY drafts "
    "older than {threshold} days to keep the fleet triageable. Reopen or "
    "rebase and push to revive.\n\n"
    "<!-- caretaker:shepherd-stale-dirty -->"
)


def _parse_dt(value: object) -> datetime | None:
    """Best-effort parse of ``updated_at`` into a timezone-aware datetime.

    Accepts strings in ISO 8601 (including the GitHub ``Z`` suffix) or an
    already-parsed ``datetime``. Returns ``None`` on failure so the caller
    can skip the PR rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class ShepherdReport:
    """Audit record for a single shepherd run.

    Mirrors the structure of ``PRTriageReport`` for consistency with the
    existing triage pipeline, but adds shepherd-specific phase outputs so
    the admin dashboard can render a per-phase summary.
    """

    # Inventory phase
    inventoried: int = 0
    enriched: int = 0
    # Dedupe phase — list of PR numbers closed as duplicates.
    closed_duplicate: list[int] = field(default_factory=list)
    # Promote phase — list of PR numbers moved from draft → ready.
    promoted: list[int] = field(default_factory=list)
    # Delta C placeholders — kept here so consumers have a stable schema
    # before the later deltas land. Empty when disabled.
    mechanical_fixed: list[int] = field(default_factory=list)
    rebased: list[int] = field(default_factory=list)
    closed_stale: list[int] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)
    # Delta F — number of LLM calls spent (0 in Delta B/C).
    llm_budget_used: int = 0
    # Delta F — one entry per LLM verdict attempted. Shape per entry:
    #   {"pr": int, "is_stuck": bool, "stuck_reason": str,
    #    "recommended_action": str, "confidence": float}
    # On a StructuredCompleteError the LLM returns None; we record the budget
    # was spent but do NOT add a verdict entry (filtered out — the caller can
    # compare ``llm_budget_used`` vs ``len(llm_verdicts)`` if they care).
    llm_verdicts: list[dict[str, object]] = field(default_factory=list)
    # Phases that were skipped because their toggle was off (for observability).
    skipped_phases: list[str] = field(default_factory=list)
    # Any errors surfaced by phase handlers, collected instead of raised so
    # one phase failure doesn't abort the rest of the loop.
    errors: list[str] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        """Total destructive/state-changing actions across all phases."""
        return (
            len(self.closed_duplicate)
            + len(self.promoted)
            + len(self.mechanical_fixed)
            + len(self.rebased)
            + len(self.closed_stale)
            + len(self.merged)
        )


async def _inventory(
    github: GitHubClient,
    owner: str,
    repo: str,
) -> list[PullRequest]:
    """Phase 1 — list open PRs + enrich with mergeStateStatus.

    Returns an empty list on transport failure; the caller appends the error
    string to ``ShepherdReport.errors`` so the run continues.

    Enrichment is best-effort — ``enrich_merge_state_status`` mutates PRs
    in-place and falls back to per-PR GraphQL calls on batch failure (see
    Delta A). The enriched ``merge_state_status`` field is consumed by later
    phases to decide between rebase / reap / merge actions.
    """
    prs = await github.list_pull_requests(owner, repo, state="open")
    if not prs:
        return []
    await github.enrich_merge_state_status(owner, repo, prs)
    return prs


async def _rebase_behind_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    prs: list[PullRequest],
    *,
    dry_run: bool,
) -> list[int]:
    """Phase 5 — call ``update-branch`` on every PR whose merge state is BEHIND.

    This is the cascade handler that fixes the #542→#539 pattern: merging
    one PR pushes its siblings BEHIND, after which GitHub refuses to merge
    them until the branch is re-synced with main. Shepherd now does this
    automatically instead of waiting for a human to re-run ``gh pr update-branch``.

    Skips non-BEHIND PRs, drafts are included (we want green drafts to be
    merge-ready after promotion), and swallows per-PR exceptions so one
    bad rebase doesn't abort the run.
    """
    rebased: list[int] = []
    for pr in prs:
        if pr.merge_state_status != MergeStateStatus.BEHIND:
            continue
        if dry_run:
            rebased.append(pr.number)
            continue
        try:
            ok = await github.update_pull_request_branch(
                owner, repo, pr.number, expected_head_sha=pr.head_sha
            )
        except Exception as exc:
            logger.warning("shepherd: update_branch #%d failed: %s", pr.number, exc)
            continue
        if ok:
            rebased.append(pr.number)
            logger.info("shepherd: rebased #%d (was BEHIND)", pr.number)
    return rebased


async def _reap_stale_dirty_drafts(
    github: GitHubClient,
    owner: str,
    repo: str,
    prs: list[PullRequest],
    *,
    threshold_days: int,
    dry_run: bool,
) -> list[int]:
    """Phase 6 — close draft PRs stuck in DIRTY (conflicted) state too long.

    ``StaleAgent`` intentionally skips drafts (``if pr.draft: continue`` at
    stale_agent/agent.py:132) on the theory that drafts signal WIP. But the
    2026-04-24 shepherd run found three draft PRs (#528, #530, #538) that
    had been DIRTY for weeks with no human intervention and no bot progress
    — exactly the signature of a fire-and-forget Copilot PR that lost its
    branch war. Those need to be reaped or they accumulate into a fleet-
    wide DIRTY pile.

    This handler uses ``merge_state_status == DIRTY`` (not a time-only
    heuristic) so we only close PRs that genuinely have a merge conflict,
    not merely-inactive drafts. The age gate (``stale_dirty_days``) keeps
    us from reaping a PR that just became DIRTY minutes ago.
    """
    closed: list[int] = []
    now = datetime.now(UTC)
    for pr in prs:
        if not pr.draft:
            continue
        if pr.merge_state_status != MergeStateStatus.DIRTY:
            continue
        updated_at = _parse_dt(pr.updated_at)
        if updated_at is None:
            logger.debug("shepherd: reaper skipping #%d (no parseable updated_at)", pr.number)
            continue
        age_days = (now - updated_at).days
        if age_days < threshold_days:
            continue
        if dry_run:
            closed.append(pr.number)
            continue
        try:
            await github.add_issue_comment(
                owner,
                repo,
                pr.number,
                _STALE_DIRTY_COMMENT.format(days=age_days, threshold=threshold_days),
            )
            await github.update_issue(owner, repo, pr.number, state="closed")
        except Exception as exc:
            logger.warning("shepherd: reap #%d failed: %s", pr.number, exc)
            continue
        closed.append(pr.number)
        logger.info("shepherd: closed stale DIRTY draft #%d (age=%d days)", pr.number, age_days)
    return closed


# Statuses that warrant an LLM opinion after mechanical phases fail to move
# the PR. Deliberately narrow — ``UNSTABLE`` and ``BEHIND`` are CI-transient
# (the next CI run or the rebase phase should clear them) so spending an LLM
# call on them is wasteful.
_LLM_ESCALATE_STATUSES: frozenset[MergeStateStatus] = frozenset(
    {MergeStateStatus.DIRTY, MergeStateStatus.BLOCKED, MergeStateStatus.UNKNOWN}
)


def _is_llm_candidate(
    pr: PullRequest,
    *,
    already_handled: frozenset[int],
) -> bool:
    """Return True if this PR warrants an LLM stuck-verdict after mechanical
    phases ran.

    Candidate rules (intentionally conservative for v1):
    1. PR was not already acted on by shepherd this run (dedupe, promote,
       rebase, stale-reap all short-circuit this).
    2. ``merge_state_status`` is DIRTY / BLOCKED / UNKNOWN. Everything else
       is either clean (no need) or CI-transient (next tick).
    """
    if pr.number in already_handled:
        return False
    status = pr.merge_state_status
    if status is None:
        return False
    return status in _LLM_ESCALATE_STATUSES


async def _escalate_stuck_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    prs: list[PullRequest],
    *,
    claude: ClaudeClient,
    budget: int,
    already_handled: frozenset[int],
    dry_run: bool,
) -> tuple[int, list[dict[str, object]], bool]:
    """Phase 8 — spend up to ``budget`` LLM calls on candidate stuck PRs.

    Returns ``(calls_spent, verdicts, exhausted)`` where:

    * ``calls_spent`` increments on every attempt, including ones that
      returned ``None`` from the LLM (prompt-cache miss, schema violation).
      This is critical: a retry loop on failures would blow past the budget
      and burn tokens with no signal.
    * ``verdicts`` collects the structured output per successful call.
    * ``exhausted`` is True iff the budget ran out *before* we iterated every
      candidate — the shepherd surfaces this as a skip tag so operators
      know to raise ``max_llm_calls_per_run`` if the backlog is chronic.

    Dry-run suppresses the LLM call entirely (shadows a no-op ``StuckVerdict``
    so the counter still advances — we want ``dry_run`` to predict real-world
    budget consumption).
    """
    calls_spent = 0
    verdicts: list[dict[str, object]] = []
    candidates = [pr for pr in prs if _is_llm_candidate(pr, already_handled=already_handled)]
    exhausted = False

    for pr in candidates:
        if calls_spent >= budget:
            exhausted = True
            break

        if dry_run:
            # Count the budget so dry-run matches real-world pacing, but do
            # not hit the LLM or the GitHub data endpoints.
            calls_spent += 1
            verdicts.append(
                {
                    "pr": pr.number,
                    "is_stuck": False,
                    "stuck_reason": "not_stuck",
                    "recommended_action": "wait",
                    "confidence": 0.0,
                    "dry_run": True,
                }
            )
            continue

        # Best-effort enrichment. All three API calls are scoped to the
        # single PR under review, so a failure at any step just means we
        # escalate with whichever signals we managed to collect.
        check_runs: list[Any] = []
        reviews: list[Any] = []
        try:
            check_runs = await github.get_check_runs(owner, repo, pr.head_sha)
        except Exception as exc:
            logger.warning("shepherd: check_runs fetch for #%d failed: %s", pr.number, exc)
        try:
            reviews = await github.get_pr_reviews(owner, repo, pr.number)
        except Exception as exc:
            logger.warning("shepherd: reviews fetch for #%d failed: %s", pr.number, exc)

        now = datetime.now(UTC)
        created_at = _parse_dt(pr.created_at)
        updated_at = _parse_dt(pr.updated_at)
        age_hours = (now - created_at).total_seconds() / 3600.0 if created_at is not None else 0.0
        last_activity_hours: float | None = (
            (now - updated_at).total_seconds() / 3600.0 if updated_at is not None else None
        )

        ctx = PRStuckContext(
            pr=pr,
            age_hours=age_hours,
            last_activity_hours=last_activity_hours,
            check_runs=check_runs,
            reviews=reviews,
            readiness_verdict=None,  # shepherd doesn't run readiness — PR agent does.
            linked_issues=[],
            repo_slug=f"{owner}/{repo}",
            collaborator_count=None,
        )

        calls_spent += 1
        try:
            verdict = await evaluate_stuck_pr_llm(ctx, claude=claude)
        except Exception as exc:
            # evaluate_stuck_pr_llm already swallows StructuredCompleteError
            # internally — anything bubbling up is a genuine bug or network
            # fault. Log, count the budget as spent, move on.
            logger.warning("shepherd: stuck-PR LLM for #%d raised: %s", pr.number, exc)
            continue

        if verdict is None:
            # Schema violation / structured-complete fell through; budget
            # was spent but no verdict recorded.
            continue

        verdicts.append(
            {
                "pr": pr.number,
                "is_stuck": verdict.is_stuck,
                "stuck_reason": verdict.stuck_reason,
                "recommended_action": verdict.recommended_action,
                "confidence": verdict.confidence,
            }
        )
        logger.info(
            "shepherd: LLM verdict for #%d is_stuck=%s reason=%s action=%s conf=%.2f",
            pr.number,
            verdict.is_stuck,
            verdict.stuck_reason,
            verdict.recommended_action,
            verdict.confidence,
        )

    # If we exhausted the candidate list without hitting the budget, we did
    # NOT "exhaust" the budget — the loop fell out naturally.
    if calls_spent < budget:
        exhausted = False
    return calls_spent, verdicts, exhausted


async def run_shepherd(
    github: GitHubClient,
    owner: str,
    repo: str,
    config: ShepherdConfig,
    *,
    dry_run: bool | None = None,
    claude: ClaudeClient | None = None,
) -> ShepherdReport:
    """Run the shepherd PR cleanup loop and return a report.

    Parameters
    ----------
    github : GitHubClient
        Authenticated client for the target repo.
    owner, repo : str
        GitHub repo coordinates.
    config : ShepherdConfig
        Phase toggles + safety knobs.
    dry_run : bool, optional
        When set, overrides ``config.dry_run``. This exists so the
        orchestrator can forward its top-level ``orchestrator.dry_run`` flag
        without mutating the config object.

    Returns
    -------
    ShepherdReport
        Structured record of what happened per phase.

    Notes
    -----
    When ``config.enabled`` is False this function returns immediately with
    an empty report. That is the byte-identical opt-out so existing
    deployments see zero behavior change until they flip the knob.

    Phase handlers are wrapped in try/except so one phase failing (e.g.
    GitHub 502 mid-run) does not abort the remaining phases; errors are
    collected into ``report.errors`` for the run summary.
    """
    report = ShepherdReport()

    if not config.enabled:
        # Opt-out path — do not even list PRs.
        report.skipped_phases.append("shepherd:disabled")
        return report

    effective_dry_run = config.dry_run if dry_run is None else dry_run

    # ── Phase 1: Inventory ─────────────────────────────────────
    try:
        open_prs = await _inventory(github, owner, repo)
    except Exception as exc:
        logger.warning("shepherd: inventory failed: %s", exc)
        report.errors.append(f"inventory: {exc}")
        return report

    report.inventoried = len(open_prs)
    # enrichment populates pr.merge_state_status in-place; count those that
    # now have a non-None value so ops can see how many enrichments landed.
    report.enriched = sum(1 for p in open_prs if p.merge_state_status is not None)

    if not open_prs:
        return report

    # ── Phase 2: Dedupe ────────────────────────────────────────
    if config.dedupe:
        try:
            closed = await close_duplicate_fix_prs(
                github, owner, repo, open_prs, dry_run=effective_dry_run
            )
            report.closed_duplicate = closed
        except Exception as exc:
            logger.warning("shepherd: dedupe failed: %s", exc)
            report.errors.append(f"dedupe: {exc}")
    else:
        report.skipped_phases.append("dedupe")

    # ── Phase 3: Promote green Copilot drafts ──────────────────
    if config.promote_drafts:
        try:
            # ready_valid_copilot_drafts skips PRs we just closed — closed PRs
            # are still in the ``open_prs`` list (we don't re-fetch), but the
            # function checks ``pr.draft and pr.is_copilot_pr`` and uses
            # combined_status before flipping, so a zombie closed PR can't be
            # promoted. Still, filter them to avoid spurious GitHub calls.
            closed_set = set(report.closed_duplicate)
            candidates = [p for p in open_prs if p.number not in closed_set]
            promoted = await ready_valid_copilot_drafts(
                github, owner, repo, candidates, dry_run=effective_dry_run
            )
            report.promoted = promoted
        except Exception as exc:
            logger.warning("shepherd: promote failed: %s", exc)
            report.errors.append(f"promote: {exc}")
    else:
        report.skipped_phases.append("promote_drafts")

    # Delta C/D/E/F phases — mechanical/merge_chain still pending later deltas.
    if not config.mechanical_fixes:
        report.skipped_phases.append("mechanical_fixes")
    else:
        report.skipped_phases.append("mechanical_fixes:pending-delta-c")

    # ── Phase 5: Rebase BEHIND PRs (Delta C) ───────────────────
    if config.auto_update_branch:
        try:
            rebased = await _rebase_behind_prs(
                github, owner, repo, open_prs, dry_run=effective_dry_run
            )
            report.rebased = rebased
        except Exception as exc:
            logger.warning("shepherd: rebase phase failed: %s", exc)
            report.errors.append(f"auto_update_branch: {exc}")
    else:
        report.skipped_phases.append("auto_update_branch")

    # ── Phase 6: Reap stale DIRTY drafts (Delta C) ─────────────
    if config.stale_dirty_reaper:
        try:
            closed_stale = await _reap_stale_dirty_drafts(
                github,
                owner,
                repo,
                open_prs,
                threshold_days=config.stale_dirty_days,
                dry_run=effective_dry_run,
            )
            report.closed_stale = closed_stale
        except Exception as exc:
            logger.warning("shepherd: stale reaper failed: %s", exc)
            report.errors.append(f"stale_dirty_reaper: {exc}")
    else:
        report.skipped_phases.append("stale_dirty_reaper")

    if not config.merge_chain:
        report.skipped_phases.append("merge_chain")
    else:
        report.skipped_phases.append("merge_chain:pending-delta-c")

    # ── Phase 8: LLM escalation for still-stuck PRs (Delta F) ──
    if config.max_llm_calls_per_run <= 0:
        report.skipped_phases.append("llm_escalation:budget-zero")
    elif claude is None:
        # LLMRouter did not hand us a Claude client (no API key / disabled
        # provider). Honour that at the shepherd layer so we don't blow up
        # downstream.
        report.skipped_phases.append("llm_escalation:no-claude")
    else:
        try:
            already_handled = frozenset(
                report.closed_duplicate + report.promoted + report.rebased + report.closed_stale
            )
            calls_spent, verdicts, exhausted = await _escalate_stuck_prs(
                github,
                owner,
                repo,
                open_prs,
                claude=claude,
                budget=config.max_llm_calls_per_run,
                already_handled=already_handled,
                dry_run=effective_dry_run,
            )
            report.llm_budget_used = calls_spent
            report.llm_verdicts = verdicts
            if exhausted:
                report.skipped_phases.append("llm_escalation:budget-exhausted")
        except Exception as exc:
            logger.warning("shepherd: llm escalation phase failed: %s", exc)
            report.errors.append(f"llm_escalation: {exc}")

    logger.info(
        "shepherd: inventoried=%d enriched=%d closed_duplicate=%d promoted=%d "
        "rebased=%d closed_stale=%d llm_budget=%d actions=%d errors=%d",
        report.inventoried,
        report.enriched,
        len(report.closed_duplicate),
        len(report.promoted),
        len(report.rebased),
        len(report.closed_stale),
        report.llm_budget_used,
        report.action_count,
        len(report.errors),
    )
    return report
