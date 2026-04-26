"""Issue-backed state tracker for orchestrator persistence."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from caretaker.causal import extract_all_causal, make_causal_id, make_causal_marker
from caretaker.github_client.api import RateLimitError
from caretaker.graph.models import NodeType, RelType
from caretaker.graph.writer import get_writer
from caretaker.observability import metrics as _metrics
from caretaker.tools.github import GitHubIssueTools

from .models import (
    IssueTrackingState,
    OrchestratorState,
    PRTrackingState,
    RunSummary,
    TrackedIssue,
    TrackedPR,
)

if TYPE_CHECKING:
    from caretaker.evolution.reflection import ReflectionResult
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

TRACKING_ISSUE_TITLE = "[Maintainer] Orchestrator State"
TRACKING_LABEL = "maintainer:internal"
STATE_MARKER_OPEN = "<!-- maintainer-state:"
STATE_MARKER_CLOSE = ":maintainer-state -->"

# Per-comment marker used by upsert_issue_comment so that the orchestrator
# state and the rolling run-history comments are edited in place instead of
# accumulating one new comment per run. portfolio#121 reached 110 bot
# comments before this was added.
STATE_COMMENT_MARKER = "<!-- caretaker:orchestrator-state -->"
RUN_HISTORY_COMMENT_MARKER = "<!-- caretaker:run-history -->"

# How many recent runs to keep visible in the rolling history comment.
_RUN_HISTORY_KEEP = 10

# Fragment that GitHub includes in the 403 response body when an issue has
# accumulated more than 2500 comments and commenting is disabled.
_COMMENT_LIMIT_FRAGMENT = "2500 comments"


def _is_comment_limit_error(exc: Exception) -> bool:
    """Return True when *exc* is the GitHub 403 'too many comments' error."""
    from caretaker.github_client.api import GitHubAPIError

    return (
        isinstance(exc, GitHubAPIError)
        and exc.status_code == 403
        and _COMMENT_LIMIT_FRAGMENT in exc.message
    )


class StateTracker:
    """Persists orchestrator state as a hidden JSON block in a tracking issue."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._issues = GitHubIssueTools(github, owner, repo)
        self._tracking_issue_number: int | None = None
        self._state: OrchestratorState = OrchestratorState()
        # Attribution: remember which (pr_number, outcome) and
        # (issue_number, outcome) tuples we've already incremented this
        # process lifetime so repeated saves of the same state don't
        # double-count. Lifetime is per-process; once the orchestrator
        # restarts we reset, which is the correct behaviour for
        # monotonic-cumulative counters whose Prometheus series already
        # reflect the history we wrote last run.
        self._emitted_pr_outcomes: dict[int, set[str]] = {}
        self._emitted_issue_outcomes: dict[int, set[str]] = {}
        self._emitted_intervention_reasons: dict[tuple[str, int], set[str]] = {}
        # Causal-chain threading. The state-tracker emits two markers per
        # save (orchestrator-state + run-history). Without parent
        # threading every save produces two root events and the Causal
        # Chain UI shows ancestors=0/descendants=0. We thread the chain
        # both *across* ticks (this tick's orchestrator-state parents the
        # previous tick's id) and *within* a tick (this tick's
        # run-history parents this tick's orchestrator-state).
        self._last_state_causal_id: str | None = None
        self._last_history_causal_id: str | None = None

    @property
    def state(self) -> OrchestratorState:
        return self._state

    async def load(self) -> OrchestratorState:
        """Load state from the tracking issue."""
        issue_number = await self._find_tracking_issue()
        if issue_number is None:
            logger.info("No tracking issue found — starting fresh")
            self._state = OrchestratorState()
            return self._state

        self._tracking_issue_number = issue_number
        comments = await self._github.get_pr_comments(self._owner, self._repo, issue_number)

        # Scrape the previous-tick causal ids out of the rolling state +
        # run-history comments so the next save can chain ``parent=`` to
        # them. This makes the Causal Chain dashboard surface a
        # continuous thread of orchestrator activity instead of one
        # root-only event per tick.
        for comment in comments:
            for marker in extract_all_causal(comment.body or ""):
                src = marker.get("source", "")
                cid = marker.get("id")
                if not cid:
                    continue
                if src == "state-tracker:orchestrator-state":
                    self._last_state_causal_id = cid
                elif src == "state-tracker:run-history":
                    self._last_history_causal_id = cid

        # Find the latest state comment (search from newest)
        for comment in reversed(comments):
            state_json = self._extract_state(comment.body)
            if state_json is not None:
                try:
                    self._state = OrchestratorState.model_validate_json(state_json)
                    logger.info("Loaded state from comment %d", comment.id)
                    return self._state
                except Exception:
                    logger.warning("Failed to parse state from comment %d", comment.id)

        logger.info("No valid state found in tracking issue — starting fresh")
        self._state = OrchestratorState()
        return self._state

    async def save(self, summary: RunSummary | None = None) -> None:
        """Save current state to the tracking issue.

        State is persisted as a single comment carrying ``STATE_COMMENT_MARKER``
        — the comment is edited in place on every save, not appended. Earlier
        versions appended a new comment per run, which produced unbounded
        comment growth on the tracking issue (portfolio#121 reached 110 bot
        comments).
        """
        if summary:
            self._state.last_run = summary
            self._state.run_history.append(summary)
            # Keep last 20 runs
            if len(self._state.run_history) > 20:
                self._state.run_history = self._state.run_history[-20:]

        try:
            if self._tracking_issue_number is None:
                await self._create_tracking_issue()
        except RateLimitError as exc:
            logger.warning(
                "State save skipped: GitHub rate-limit while creating tracking issue (%s). "
                "In-memory state is intact; next successful run will persist.",
                exc,
            )
            return

        assert self._tracking_issue_number is not None

        state_json = self._state.model_dump_json(indent=2)
        body = self._build_state_comment(state_json, summary)
        try:
            await self._github.upsert_issue_comment(
                self._owner,
                self._repo,
                self._tracking_issue_number,
                STATE_COMMENT_MARKER,
                body,
            )
        except RateLimitError as exc:
            logger.warning(
                "State save skipped: GitHub rate-limit while upserting state comment (%s). "
                "In-memory state is intact; next successful run will persist.",
                exc,
            )
            return
        except Exception as exc:
            if _is_comment_limit_error(exc):
                await self._rotate_tracking_issue()
                await self._github.upsert_issue_comment(
                    self._owner,
                    self._repo,
                    self._tracking_issue_number,
                    STATE_COMMENT_MARKER,
                    body,
                )
            else:
                raise
        logger.info("State saved to tracking issue #%d", self._tracking_issue_number)
        # Attribution telemetry (R&D workstream A2). Emitted at save time
        # rather than at action time so the counter increments reflect
        # "what's persisted" (the source of truth) rather than "what an
        # agent tried to do." This means a rate-limited save skips the
        # emission; the next successful save picks up the backlog because
        # the in-memory classifier compares the current snapshot to the
        # emitted-outcome set on the tracker.
        self._emit_attribution_metrics()
        if summary is not None:
            self._emit_run_graph(summary)

    # ── Attribution telemetry helpers ───────────────────────────────────

    def _repo_slug(self) -> str:
        return f"{self._owner}/{self._repo}"

    @staticmethod
    def classify_pr_outcomes(tracking: TrackedPR) -> frozenset[str]:
        """Classify a tracked PR into the attribution outcome set.

        A single PR can fall into multiple outcomes simultaneously:
        ``merged`` implies ``touched``; ``operator_rescued`` is
        orthogonal to ``touched`` / ``merged`` (a human can push work
        after caretaker merges too, though it's rare).

        Returns a ``frozenset`` of label values drawn from
        :data:`~caretaker.observability.metrics.PR_OUTCOMES`.
        """
        outcomes: set[str] = set()
        if tracking.caretaker_touched:
            outcomes.add("touched")
        if tracking.caretaker_merged:
            outcomes.add("merged")
        # ``closed_unmerged`` is "caretaker closed the PR but didn't merge
        # it" — e.g. the CI backlog guard. Distinguish from a human close
        # via ``caretaker_touched``: if caretaker never touched, the
        # closure can't be attributed to us.
        if (
            tracking.caretaker_touched
            and tracking.state == PRTrackingState.CLOSED
            and not tracking.caretaker_merged
        ):
            outcomes.add("closed_unmerged")
        if tracking.operator_intervened:
            outcomes.add("operator_rescued")
        if tracking.state == PRTrackingState.ESCALATED and not tracking.operator_intervened:
            # Escalated without a human rescue means caretaker gave up
            # and no operator has acted yet — this is the "abandoned"
            # bucket in the dashboard: a PR caretaker couldn't help on.
            outcomes.add("abandoned")
        return frozenset(outcomes)

    @staticmethod
    def classify_issue_outcomes(tracking: TrackedIssue) -> frozenset[str]:
        """Classify a tracked issue into the attribution outcome set."""
        outcomes: set[str] = set()
        if tracking.caretaker_touched:
            outcomes.add("triaged")
        if tracking.caretaker_closed:
            # Distinguish stale closes (time-based triage) from
            # caretaker's active closes (duplicate / question). This
            # matches the dashboard taxonomy: ``stale_closed`` is an
            # auto-hygiene signal, ``closed_by_caretaker`` is real work.
            if tracking.state == IssueTrackingState.STALE:
                outcomes.add("stale_closed")
            else:
                outcomes.add("closed_by_caretaker")
        elif tracking.state == IssueTrackingState.CLOSED and tracking.caretaker_touched:
            # Caretaker touched the issue but a human closed it — counts
            # as an operator close under the attribution lens.
            outcomes.add("closed_by_operator")
        return frozenset(outcomes)

    def _emit_attribution_metrics(self) -> None:
        """Increment attribution counters for new outcomes on tracked rows.

        Called from :meth:`save`. Skips outcomes already emitted this
        process for the same ``(kind, number)`` key so repeated saves of
        the same state don't double-count.
        """
        repo = self._repo_slug()
        for pr_number, pr in self._state.tracked_prs.items():
            current = self.classify_pr_outcomes(pr)
            already = self._emitted_pr_outcomes.setdefault(pr_number, set())
            for outcome in current - already:
                _metrics.record_pr_outcome(repo, outcome)
                already.add(outcome)
            # Intervention reasons are emitted from a parallel structure
            # because a PR can collect multiple reasons over its
            # lifetime; dedup per ``(repo, pr_number, reason)``.
            if pr.intervention_reasons:
                seen = self._emitted_intervention_reasons.setdefault(("pr", pr_number), set())
                for reason in pr.intervention_reasons:
                    if reason in seen:
                        continue
                    _metrics.record_operator_intervention(repo, reason)
                    seen.add(reason)
        for issue_number, issue in self._state.tracked_issues.items():
            current = self.classify_issue_outcomes(issue)
            already = self._emitted_issue_outcomes.setdefault(issue_number, set())
            for outcome in current - already:
                _metrics.record_issue_outcome(repo, outcome)
                already.add(outcome)
            if issue.intervention_reasons:
                seen = self._emitted_intervention_reasons.setdefault(("issue", issue_number), set())
                for reason in issue.intervention_reasons:
                    if reason in seen:
                        continue
                    _metrics.record_operator_intervention(repo, reason)
                    seen.add(reason)

    def _emit_run_graph(self, summary: RunSummary) -> None:
        """Publish the just-saved run to the event-driven graph writer.

        Called at the end of :meth:`save` so a dropped graph write cannot
        prevent state from being persisted to the tracking issue — GitHub
        is the source of truth, Neo4j is a projection.
        """
        run_id = f"run:{summary.run_at.isoformat()}"
        writer = get_writer()
        writer.record_node(
            NodeType.RUN,
            run_id,
            {
                "name": f"run-{summary.run_at.strftime('%Y%m%dT%H%M%S')}",
                "run_at": summary.run_at.isoformat(),
                "mode": summary.mode,
                "prs_monitored": summary.prs_monitored,
                "prs_merged": summary.prs_merged,
                "issues_triaged": summary.issues_triaged,
                "goal_health": summary.goal_health if summary.goal_health is not None else 0.0,
                "escalation_rate": summary.escalation_rate,
                "repo": f"{self._owner}/{self._repo}",
                "valid_from": summary.run_at.isoformat(),
            },
        )
        if summary.goal_health is not None:
            # Ensure the synthetic aggregate goal node exists before
            # the edge merge — the GraphBuilder full-sync also merges
            # it but live writes can land before the first sync.
            writer.record_node(
                NodeType.GOAL,
                "goal:overall",
                {"name": "overall", "aggregate": True},
            )
            # AFFECTED is the M2 edge — semantically "this run moved the
            # goal score by this much." `valid_from` is the run's wall
            # clock; `valid_to` stays unset because the score only
            # ceases to reflect a given run's contribution when a newer
            # run supersedes it, and that's an analysis-layer concern.
            writer.record_edge(
                NodeType.RUN,
                run_id,
                NodeType.GOAL,
                "goal:overall",
                RelType.AFFECTED,
                {
                    "score": summary.goal_health,
                    "escalation_rate": summary.escalation_rate,
                    "valid_from": summary.run_at.isoformat(),
                },
            )

    async def post_run_summary(self, summary: RunSummary) -> None:
        """Post a human-readable run summary to the tracking issue.

        Maintains a single rolling "Maintainer Run History" comment with the
        last :data:`_RUN_HISTORY_KEEP` runs rendered, edited in place. This
        replaces the previous append-per-run pattern that drove unbounded
        comment growth.
        """
        try:
            if self._tracking_issue_number is None:
                await self._create_tracking_issue()
        except RateLimitError as exc:
            logger.warning(
                "Run summary post skipped: GitHub rate-limit while creating tracking issue (%s)",
                exc,
            )
            return
        assert self._tracking_issue_number is not None

        recent = list(self._state.run_history[-_RUN_HISTORY_KEEP:])
        # Make sure the latest run is represented even if save() didn't run
        # for some reason (defensive — save() should always be called first).
        if not recent or recent[-1] is not summary:
            recent.append(summary)
            recent = recent[-_RUN_HISTORY_KEEP:]

        body = self._build_history_comment(recent)
        try:
            await self._github.upsert_issue_comment(
                self._owner,
                self._repo,
                self._tracking_issue_number,
                RUN_HISTORY_COMMENT_MARKER,
                body,
            )
        except RateLimitError as exc:
            logger.warning(
                "Run summary post skipped: GitHub rate-limit while upserting history comment (%s)",
                exc,
            )
            return
        except Exception as exc:
            if _is_comment_limit_error(exc):
                await self._rotate_tracking_issue()
                await self._github.upsert_issue_comment(
                    self._owner,
                    self._repo,
                    self._tracking_issue_number,
                    RUN_HISTORY_COMMENT_MARKER,
                    body,
                )
            else:
                raise

    async def post_reflection(self, result: ReflectionResult) -> None:
        """Post a reflection analysis comment to the tracking issue."""
        from caretaker.evolution.reflection import format_reflection_comment

        if self._tracking_issue_number is None:
            await self._create_tracking_issue()
        assert self._tracking_issue_number is not None

        body = format_reflection_comment(result)
        try:
            await self._issues.comment(self._tracking_issue_number, body)
        except Exception as exc:
            if _is_comment_limit_error(exc):
                await self._rotate_tracking_issue()
                await self._issues.comment(self._tracking_issue_number, body)
            else:
                raise
        logger.info(
            "Reflection posted to tracking issue #%d (triggered_by=%s)",
            self._tracking_issue_number,
            result.triggered_by,
        )

    async def _find_tracking_issue(self) -> int | None:
        issues = await self._issues.list(labels=TRACKING_LABEL)
        for issue in issues:
            if issue.title == TRACKING_ISSUE_TITLE:
                return issue.number
        return None

    async def _create_tracking_issue(self) -> None:
        issue = await self._issues.create(
            title=TRACKING_ISSUE_TITLE,
            body=(
                "This issue is used by the caretaker orchestrator "
                "to track state and post run summaries.\n\n"
                "**Do not close or modify this issue.**\n\n"
                "Label: `maintainer:internal`"
            ),
            labels=[TRACKING_LABEL],
        )
        self._tracking_issue_number = issue.number
        logger.info("Created tracking issue #%d", issue.number)

    async def _rotate_tracking_issue(self) -> None:
        """Close the full tracking issue and create a fresh replacement.

        Called automatically when GitHub returns a 403 because the current
        tracking issue has accumulated more than 2500 comments.  The old
        issue is closed with an explanatory note; a new one is opened and
        :attr:`_tracking_issue_number` is updated so the next save lands on
        the new issue.
        """
        old_number = self._tracking_issue_number
        if old_number is not None:
            with contextlib.suppress(Exception):
                await self._github.update_issue(
                    self._owner,
                    self._repo,
                    old_number,
                    state="closed",
                    body=(
                        "This tracking issue has reached GitHub's 2500-comment "
                        "limit.  A replacement tracking issue has been created "
                        "automatically — please use that one going forward."
                    ),
                )
            logger.warning(
                "Tracking issue #%d is full (≥2500 comments) — "
                "closed and creating a new tracking issue",
                old_number,
            )
        self._tracking_issue_number = None
        await self._create_tracking_issue()

    @staticmethod
    def _extract_state(body: str) -> str | None:
        start = body.find(STATE_MARKER_OPEN)
        if start == -1:
            return None
        start += len(STATE_MARKER_OPEN)
        end = body.find(STATE_MARKER_CLOSE, start)
        if end == -1:
            return None
        return body[start:end].strip()

    def _build_state_comment(self, state_json: str, summary: RunSummary | None = None) -> str:
        # Thread to the previous tick's orchestrator-state event so the
        # state lineage forms a chain across save ticks. The ``causal_id``
        # is minted up-front so we can stash it on the instance and use
        # it as the parent for *this tick's* run-history marker.
        cid = make_causal_id("state-tracker:orchestrator-state")
        marker = make_causal_marker(
            "state-tracker:orchestrator-state",
            parent=self._last_state_causal_id,
            causal_id=cid,
        )
        self._last_state_causal_id = cid
        lines = [
            STATE_COMMENT_MARKER,
            marker,
            "",
            "## Orchestrator State Update\n",
        ]
        if summary:
            lines.append(f"Run completed at {summary.run_at.isoformat()} (mode: {summary.mode})")
            lines.append(f"- PRs monitored: {summary.prs_monitored}")
            lines.append(f"- PRs merged: {summary.prs_merged}")
            lines.append(f"- Issues triaged: {summary.issues_triaged}")
            if summary.errors:
                lines.append(f"- Errors: {len(summary.errors)}")
            if summary.goal_health is not None:
                lines.append(f"- Goal health: {summary.goal_health:.2%}")
                if summary.goal_escalation_count:
                    lines.append(f"- Goal escalations: {summary.goal_escalation_count}")
            lines.append("")
        lines.append(f"{STATE_MARKER_OPEN}\n{state_json}\n{STATE_MARKER_CLOSE}")
        return "\n".join(lines)

    def _build_history_comment(self, runs: list[RunSummary]) -> str:
        """Render a rolling history of recent runs as one upsertable body.

        Threads the ``state-tracker:run-history`` causal marker to *this
        tick's* ``state-tracker:orchestrator-state`` id (stashed on the
        instance by :meth:`_build_state_comment`) and then to the
        *previous tick's* run-history id when no fresh state id is
        available. The result is a connected causal chain instead of
        two parallel root events per save.
        """
        cid = make_causal_id("state-tracker:run-history")
        parent = self._last_state_causal_id or self._last_history_causal_id
        marker = make_causal_marker(
            "state-tracker:run-history",
            parent=parent,
            causal_id=cid,
        )
        self._last_history_causal_id = cid
        lines = [
            RUN_HISTORY_COMMENT_MARKER,
            marker,
            "",
            f"## Maintainer Run History (last {len(runs)} runs)",
            "",
            "_This comment is edited in place on every run — it does not grow._",
            "",
        ]
        for run in reversed(runs):  # newest first
            lines.append(self._format_summary(run))
            lines.append("---")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _format_summary(summary: RunSummary) -> str:
        lines = [
            f"## Maintainer Run — {summary.run_at.strftime('%B %d, %Y')}",
            "",
            "### PR Agent",
            f"- {summary.prs_monitored} PRs monitored",
            f"- {summary.prs_merged} merged",
            f"- {summary.prs_escalated} escalated",
            f"- {summary.prs_fix_requested} fix requests posted",
            "",
            "### Issue Agent",
            f"- {summary.issues_triaged} issues triaged",
            f"- {summary.issues_assigned} assigned to Copilot",
            f"- {summary.issues_closed} closed",
            f"- {summary.issues_escalated} escalated",
            "",
            "### Reconciliation",
            f"- {summary.orphaned_prs} orphaned PRs detected",
            f"- {summary.stale_assignments_escalated} stale assignments escalated",
            f"- Escalation rate: {summary.escalation_rate:.2%}",
            f"- Copilot success rate: {summary.copilot_success_rate:.2%}",
            f"- Avg time-to-merge: {summary.avg_time_to_merge_hours:.2f}h",
            "",
        ]
        if summary.upgrade_available:
            lines.extend(
                [
                    "### Upgrade Agent",
                    f"- Upgrade available: {summary.upgrade_version}",
                    "",
                ]
            )
        if any(
            (
                summary.charlie_managed_issues,
                summary.charlie_managed_prs,
                summary.charlie_issues_closed,
                summary.charlie_prs_closed,
            )
        ):
            lines.extend(
                [
                    "### Charlie Agent",
                    f"- {summary.charlie_managed_issues} managed issues reviewed",
                    f"- {summary.charlie_managed_prs} managed PRs reviewed",
                    f"- {summary.charlie_issues_closed} issues closed",
                    f"- {summary.charlie_prs_closed} PRs closed",
                    f"- {summary.charlie_duplicates_closed} duplicate items cleaned up",
                    "",
                ]
            )
        if summary.errors:
            lines.extend(
                [
                    "### Errors",
                    *[f"- {e}" for e in summary.errors],
                    "",
                ]
            )
        if summary.goal_health is not None:
            lines.extend(
                [
                    "### Goal Health",
                    f"- Overall health: {summary.goal_health:.2%}",
                    f"- Goal escalations: {summary.goal_escalation_count}",
                    "",
                ]
            )
        return "\n".join(lines)
