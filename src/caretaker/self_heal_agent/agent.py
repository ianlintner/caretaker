"""Self-heal Agent — triages caretaker's own workflow failures and self-repairs."""

from __future__ import annotations

import contextlib
import gzip
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from caretaker import __version__
from caretaker.causal import make_causal_marker
from caretaker.self_heal_agent.upstream_reporter import (
    report_upstream_bug,
    report_upstream_feature,
)
from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue
    from caretaker.memory.embeddings import Embedder
    from caretaker.memory.retriever import MemoryRetriever
    from caretaker.self_heal_agent.fix_ladder import FixLadderResult
    from caretaker.self_heal_agent.sandbox import FixLadderSandbox

logger = logging.getLogger(__name__)

SELF_HEAL_LABEL = "caretaker:self-heal"
SELF_HEAL_MARKER = "<!-- caretaker:self-heal -->"

# Workflow name for the caretaker dogfood run (as seen in workflow_run events)
CARETAKER_WORKFLOW_NAMES = {"Caretaker", "caretaker", "maintainer"}


class FailureKind(StrEnum):
    CONFIG_ERROR = "config_error"
    INTEGRATION_ERROR = "integration_error"
    UPSTREAM_BUG = "upstream_bug"
    MISSING_FEATURE = "missing_feature"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


@dataclass
class SelfHealReport:
    failures_analyzed: int = 0
    local_issues_created: list[int] = field(default_factory=list)
    upstream_issues_opened: list[int] = field(default_factory=list)
    upstream_features_requested: list[int] = field(default_factory=list)
    auto_fixed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Sigs that were actioned this run — used to update persisted state dedup
    actioned_sigs: list[str] = field(default_factory=list)
    # Updated cooldown map to persist back to state
    updated_cooldowns: dict[str, str] = field(default_factory=dict)
    # Fix-ladder outcomes keyed by ``error_signature`` — surfaced to the
    # orchestrator so dashboards can show "ladder fixed X, escalated Y".
    fix_ladder_outcomes: dict[str, str] = field(default_factory=dict)
    # PR numbers opened by the fix ladder for ``fixed`` / ``partial``
    # outcomes. Kept distinct from ``local_issues_created`` so the
    # metrics layer doesn't conflate the two.
    fix_ladder_prs: list[int] = field(default_factory=list)


class SelfHealAgent:
    """
    Monitors caretaker's own workflow runs.

    When a caretaker run fails it:
    1. Parses the failure log to classify the problem
    2. If it's a local config / integration issue → creates a fix issue assigned to @copilot
    3. If it looks like a caretaker library bug → opens a bug report upstream
    4. If a feature is obviously missing → opens a feature request upstream
    5. Transient failures are noted but not actioned
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        report_upstream: bool = True,
        known_sigs: set[str] | None = None,
        cooldown_hours: int = 6,
        issue_cooldowns: dict[str, str] | None = None,
        max_open_per_hour: int = 5,
        max_open_per_day: int = 20,
        # ── Wave A3: fix-ladder wiring ───────────────────────────────
        # All optional so existing call-sites keep the legacy
        # escalation-only flow until explicitly opted-in via
        # ``SelfHealAgentConfig.fix_ladder.enabled``.
        fix_ladder_enabled: bool = False,
        fix_ladder_sandbox: FixLadderSandbox | None = None,
        fix_ladder_max_rungs: int = 6,
        fix_ladder_base_branch: str = "main",
        fix_ladder_branch_prefix: str = "caretaker/fix-ladder",
        fix_ladder_pr_label: str = "caretaker:fix-ladder",
        memory_retriever: MemoryRetriever | None = None,
        memory_embedder: Embedder | None = None,
        write_embeddings: bool = False,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._report_upstream = report_upstream
        self._issues = GitHubIssueTools(github, owner, repo)
        # Pre-seeded sigs from persisted state (survive issue close/reopen cycles)
        self._known_sigs: set[str] = set(known_sigs or [])
        self._cooldown_hours = cooldown_hours
        # Mutable copy — callers read back updated_cooldowns after run()
        self._issue_cooldowns: dict[str, str] = dict(issue_cooldowns or {})
        # Storm caps — refuse to open more than N self-heal issues in any
        # rolling hour / day window. Counts existing self-heal-labeled issues
        # by their createdAt so the cap survives across workflow runs without
        # extra state plumbing. 0 disables the respective check.
        self._max_open_per_hour = max(0, max_open_per_hour)
        self._max_open_per_day = max(0, max_open_per_day)
        # Fix-ladder state — the ladder runs before the legacy escalation
        # path when ``fix_ladder_enabled`` is true AND a sandbox is wired.
        self._fix_ladder_enabled = fix_ladder_enabled
        self._fix_ladder_sandbox = fix_ladder_sandbox
        self._fix_ladder_max_rungs = max(1, fix_ladder_max_rungs)
        self._fix_ladder_base_branch = fix_ladder_base_branch
        self._fix_ladder_branch_prefix = fix_ladder_branch_prefix
        self._fix_ladder_pr_label = fix_ladder_pr_label
        self._memory_retriever = memory_retriever
        self._memory_embedder = memory_embedder
        self._write_embeddings = write_embeddings

    async def run(self, event_payload: dict[str, Any] | None = None) -> SelfHealReport:
        """Analyse caretaker workflow failures."""
        report = SelfHealReport()

        # Extract workflow run_id for grouping related issues
        run_id: int | None = None
        if event_payload and event_payload.get("workflow_run"):
            run_id = event_payload["workflow_run"].get("id")

        failure_logs = await self._collect_failure_logs(event_payload)
        if not failure_logs:
            logger.info("Self-heal agent: no caretaker workflow failures found")
            return report

        report.failures_analyzed = len(failure_logs)
        logger.info("Self-heal agent: %d failure(s) to analyse", len(failure_logs))

        # Merge open-issue sigs with pre-seeded state sigs for robust dedup
        existing_sigs = await self._get_existing_self_heal_sigs()
        existing_sigs |= self._known_sigs

        # Cross-agent run_id dedup: if a devops issue already exists for
        # this workflow run, skip creating self-heal issues entirely.
        if run_id and await self._run_id_already_tracked(run_id):
            logger.info("Self-heal: run_id %d already tracked by another agent, skipping", run_id)
            return report

        # Storm cap (per-sig, per hour_window bucket) — pre-compute the
        # histogram once per run so the inner loop is a dict lookup. See
        # ``_storm_cap_histogram`` for the keying decision (T-M7).
        sig_histogram = await self._storm_cap_histogram()

        for job_name, log_text in failure_logs:
            kind, title, details = _classify_failure(job_name, log_text)
            sig = _sig(job_name, kind, title)

            if sig in existing_sigs:
                logger.debug("Self-heal: skipping duplicate for %s", job_name)
                continue

            blocked, reason = self._sig_storm_cap_blocked(sig, sig_histogram)
            if blocked:
                logger.warning(
                    "Self-heal: %s/%s sig=%s %s",
                    self._owner,
                    self._repo,
                    sig,
                    reason,
                )
                report.errors.append(f"storm-cap: {reason}")
                continue

            # Cooldown: same job+kind recently actioned → skip even with a different sig
            coarse_key = f"self-heal:{job_name}:{kind.value}"
            if self._is_on_cooldown(coarse_key):
                logger.info("Self-heal: cooldown active for %s, skipping", coarse_key)
                continue

            logger.info("Self-heal: job=%s kind=%s", job_name, kind.value)

            if kind == FailureKind.TRANSIENT:
                logger.info("Self-heal: transient failure in %s — no action", job_name)
                continue

            # ── Wave A3: deterministic-first fix ladder ───────────
            # Runs before the legacy escalation path. When the ladder
            # fully resolves the incident we skip the issue entirely;
            # on ``partial`` / ``escalated`` we still record the
            # escalation prompt so the downstream issue body carries
            # the context. ``no_op`` / ``error`` fall through to the
            # pre-existing behaviour.
            ladder_outcome: str | None = None
            ladder_escalation: str | None = None
            if self._fix_ladder_enabled and self._fix_ladder_sandbox is not None:
                ladder_result = await self._maybe_run_fix_ladder(
                    job_name=job_name,
                    kind=kind,
                    title=title,
                    log_text=log_text,
                    sig=sig,
                    run_id=run_id,
                )
                if ladder_result is not None:
                    ladder_outcome = ladder_result.outcome
                    report.fix_ladder_outcomes[sig] = ladder_outcome
                    if ladder_outcome == "fixed":
                        # Ladder closed the loop — skip issue creation.
                        report.actioned_sigs.append(sig)
                        self._record_cooldown(coarse_key)
                        continue
                    if ladder_outcome == "partial":
                        # PR opened (if possible) but the prompt still
                        # needs to ride along on the follow-up issue.
                        ladder_escalation = ladder_result.escalation_prompt
                    elif ladder_outcome == "escalated":
                        ladder_escalation = ladder_result.escalation_prompt

            if kind in (FailureKind.CONFIG_ERROR, FailureKind.INTEGRATION_ERROR):
                # Create a local fix issue assigned to @copilot
                try:
                    issue = await self._create_local_fix_issue(
                        job_name,
                        kind,
                        title,
                        details,
                        log_text,
                        sig,
                        run_id=run_id,
                        ladder_escalation=ladder_escalation,
                    )
                    report.local_issues_created.append(issue.number)
                    report.actioned_sigs.append(sig)
                    self._record_cooldown(coarse_key)
                except Exception as e:
                    logger.error("Self-heal: failed to create local issue: %s", e)
                    report.errors.append(str(e))

            elif kind == FailureKind.UPSTREAM_BUG and self._report_upstream:
                upstream = await report_upstream_bug(
                    github=self._github,
                    title=title,
                    description=details,
                    context=log_text[-2000:],
                    caretaker_version=__version__,
                    reporter_repo=f"{self._owner}/{self._repo}",
                )
                if not upstream.skipped and upstream.issue_number:
                    report.upstream_issues_opened.append(upstream.issue_number)
                    report.actioned_sigs.append(sig)
                    self._record_cooldown(coarse_key)
                    # Also create a local tracking issue referencing upstream
                    try:
                        issue = await self._create_local_tracking_issue(
                            job_name,
                            title,
                            upstream.issue_number,
                            log_text,
                            sig,
                            run_id=run_id,
                        )
                        report.local_issues_created.append(issue.number)
                    except Exception as e:
                        logger.warning("Self-heal: tracking issue failed: %s", e)

            elif kind == FailureKind.MISSING_FEATURE and self._report_upstream:
                upstream = await report_upstream_feature(
                    github=self._github,
                    title=title,
                    description=details,
                    caretaker_version=__version__,
                    reporter_repo=f"{self._owner}/{self._repo}",
                )
                if not upstream.skipped and upstream.issue_number:
                    report.upstream_features_requested.append(upstream.issue_number)
                    report.actioned_sigs.append(sig)
                    self._record_cooldown(coarse_key)

            else:
                # UNKNOWN — create a local investigation issue
                try:
                    issue = await self._create_local_fix_issue(
                        job_name,
                        FailureKind.UNKNOWN,
                        title,
                        details,
                        log_text,
                        sig,
                        run_id=run_id,
                        ladder_escalation=ladder_escalation,
                    )
                    report.local_issues_created.append(issue.number)
                    report.actioned_sigs.append(sig)
                    self._record_cooldown(coarse_key)
                except Exception as e:
                    report.errors.append(str(e))

        report.updated_cooldowns = dict(self._issue_cooldowns)
        return report

    # ── Private helpers ─────────────────────────────────────────────────────

    def _is_on_cooldown(self, coarse_key: str) -> bool:
        """Return True if an issue was recently created for this coarse key."""
        ts_str = self._issue_cooldowns.get(coarse_key)
        if not ts_str:
            return False
        try:
            last_created = datetime.fromisoformat(ts_str)
            if last_created.tzinfo is None:
                last_created = last_created.replace(tzinfo=UTC)
            elapsed_hours = (datetime.now(UTC) - last_created).total_seconds() / 3600
            return elapsed_hours < self._cooldown_hours
        except (ValueError, TypeError):
            return False

    def _record_cooldown(self, coarse_key: str) -> None:
        """Record that an issue was just created for this coarse key."""
        self._issue_cooldowns[coarse_key] = datetime.now(UTC).isoformat()

    async def _collect_failure_logs(
        self, event_payload: dict[str, Any] | None
    ) -> list[tuple[str, str]]:
        """Return [(job_name, log_text)] for failed caretaker workflow jobs."""
        results: list[tuple[str, str]] = []

        if event_payload and event_payload.get("workflow_run"):
            run = event_payload["workflow_run"]
            workflow_name = run.get("name", "")
            if workflow_name not in CARETAKER_WORKFLOW_NAMES:
                return []
            if run.get("conclusion") not in ("failure", "timed_out"):
                return []

            run_id = run["id"]
            jobs_data = await self._github._get(
                f"/repos/{self._owner}/{self._repo}/actions/runs/{run_id}/jobs"
            )
            if not jobs_data:
                return []

            for job in jobs_data.get("jobs", []):
                if job.get("conclusion") not in ("failure", "timed_out"):
                    continue
                log = await self._fetch_job_log(job["id"])
                results.append((job["name"], log))
            return results

        # Fallback: inspect the most recent workflow run for "Caretaker" workflow
        runs_data = await self._github._get(
            f"/repos/{self._owner}/{self._repo}/actions/workflows",
        )
        if not runs_data:
            return []

        caretaker_workflow_id: int | None = None
        for wf in runs_data.get("workflows", []):
            if wf.get("name") in CARETAKER_WORKFLOW_NAMES:
                caretaker_workflow_id = wf["id"]
                break

        if caretaker_workflow_id is None:
            return []

        recent_runs = await self._github._get(
            f"/repos/{self._owner}/{self._repo}/actions/workflows/{caretaker_workflow_id}/runs",
            params={"per_page": 5, "status": "failure"},
        )
        if not recent_runs:
            return []

        for run in (recent_runs.get("workflow_runs") or [])[:1]:
            run_id = run["id"]
            jobs_data = await self._github._get(
                f"/repos/{self._owner}/{self._repo}/actions/runs/{run_id}/jobs"
            )
            if not jobs_data:
                continue
            for job in jobs_data.get("jobs", []):
                if job.get("conclusion") not in ("failure", "timed_out"):
                    continue
                log = await self._fetch_job_log(job["id"])
                results.append((job["name"], log))

        return results

    async def _fetch_job_log(self, job_id: int) -> str:
        try:
            token = await self._github._creds.default_token()
            resp = await self._github._client.get(
                f"/repos/{self._owner}/{self._repo}/actions/jobs/{job_id}/logs",
                follow_redirects=True,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return _decode_job_log_payload(resp.content, resp.text)
        except Exception as e:
            logger.debug("Self-heal: could not fetch job log %s: %s", job_id, e)
        return ""

    async def _storm_cap_histogram(self) -> dict[str, dict[str, int]]:
        """Build a per-sig histogram of existing self-heal issues.

        The key is the per-run error signature (see :func:`_sig`). The
        value is ``{"current_hour": int, "day": int, "hour_window": int}``
        where ``current_hour`` counts issues created in the *current*
        tumbling hour window (``floor(now / 3600)``) and ``day`` counts
        issues created in the last 24h.

        Sprint-2 intent: key storm caps on ``(repo_slug, error_sig,
        hour_window)`` so one recurring failure can't burn the whole
        per-repo budget. The repo dimension is implicit — a
        :class:`SelfHealAgent` instance is bound to one ``(owner, repo)``
        via ``self._issues``.
        """
        empty: dict[str, dict[str, int]] = {}
        if self._max_open_per_hour <= 0 and self._max_open_per_day <= 0:
            return empty

        try:
            issues = await self._issues.list(state="open", labels=SELF_HEAL_LABEL)
        except Exception as e:
            logger.warning("Storm cap: failed to list self-heal issues (%s) — allowing", e)
            return empty

        from datetime import UTC, timedelta
        from datetime import datetime as _dt

        now = _dt.now(UTC)
        current_hour_window = int(now.timestamp() // 3600)
        day_ago = now - timedelta(days=1)

        hist: dict[str, dict[str, int]] = {}
        for issue in issues:
            created = getattr(issue, "created_at", None)
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < day_ago:
                continue
            sig = _extract_sig_from_body(getattr(issue, "body", "") or "")
            if not sig:
                continue
            bucket = hist.setdefault(sig, {"current_hour": 0, "day": 0})
            bucket["day"] += 1
            issue_hour_window = int(created.timestamp() // 3600)
            if issue_hour_window == current_hour_window:
                bucket["current_hour"] += 1
        return hist

    def _sig_storm_cap_blocked(
        self, sig: str, histogram: dict[str, dict[str, int]]
    ) -> tuple[bool, str]:
        """Return (blocked, reason) for a specific ``sig``.

        Checks the hour-window count + 24h count against the configured
        caps. Cap ``<= 0`` disables that axis.
        """
        bucket = histogram.get(sig, {"current_hour": 0, "day": 0})
        hour_count = bucket.get("current_hour", 0)
        day_count = bucket.get("day", 0)

        if self._max_open_per_hour > 0 and hour_count >= self._max_open_per_hour:
            return True, (
                f"hourly cap hit ({hour_count}/{self._max_open_per_hour} self-heal "
                f"issues for sig={sig} in this hour window) — pausing"
            )
        if self._max_open_per_day > 0 and day_count >= self._max_open_per_day:
            return True, (
                f"daily cap hit ({day_count}/{self._max_open_per_day} self-heal "
                f"issues for sig={sig} in the last 24h) — pausing"
            )
        return False, ""

    async def _get_existing_self_heal_sigs(self) -> set[str]:
        issues = await self._issues.list(state="open", labels=SELF_HEAL_LABEL)
        sigs: set[str] = set()
        for issue in issues:
            sig = _extract_sig_from_body(issue.body or "")
            if sig:
                sigs.add(sig)
        return sigs

    async def _run_id_already_tracked(self, run_id: int) -> bool:
        """Check if any open issue (any agent) already references this run_id."""
        return await self._issues.run_id_tracked(run_id, [SELF_HEAL_LABEL, "devops:build-failure"])

    async def _create_local_fix_issue(
        self,
        job_name: str,
        kind: FailureKind,
        title: str,
        details: str,
        log_text: str,
        sig: str,
        *,
        run_id: int | None = None,
        ladder_escalation: str | None = None,
    ) -> Issue:
        full_title = f"🩺 Caretaker self-heal: {title}"
        body = _build_fix_issue_body(
            job_name,
            kind,
            title,
            details,
            log_text,
            sig,
            run_id=run_id,
            ladder_escalation=ladder_escalation,
        )
        await self._ensure_label(SELF_HEAL_LABEL, "0075ca", "Caretaker self-heal: fix needed")
        return await self._issues.create(
            title=full_title,
            body=body,
            labels=[SELF_HEAL_LABEL, "bug"],
            assignees=["copilot"],
            copilot_assignment=self._issues.default_copilot_assignment(),
        )

    async def _maybe_run_fix_ladder(
        self,
        *,
        job_name: str,
        kind: FailureKind,
        title: str,
        log_text: str,
        sig: str,
        run_id: int | None,
    ) -> FixLadderResult | None:
        """Run the deterministic fix ladder and record outcomes.

        Returns ``None`` when the ladder is not configured or when
        the sandbox fails to initialise — callers then fall back to
        the legacy escalation path. Otherwise returns the full
        :class:`FixLadderResult`; the caller branches on
        ``result.outcome`` to decide whether to skip or to forward
        the escalation prompt.
        """
        # Local imports so the self-heal module stays importable in
        # environments that don't have the sandbox (e.g. the MCP
        # server boot path) without pulling ``subprocess`` and the
        # pydantic models every time.
        from caretaker.memory.core import IncidentMemory, publish_incident_with_embedding
        from caretaker.observability.metrics import (
            record_fix_ladder_escalation,
            record_fix_ladder_outcome,
        )
        from caretaker.self_heal_agent.fix_ladder import Incident, run_fix_ladder
        from caretaker.self_heal_agent.fix_ladder_pr import open_fix_ladder_pr

        if self._fix_ladder_sandbox is None:
            return None

        incident = Incident(
            error_signature=sig,
            kind=kind.value,
            log_tail=log_text[-8000:],
            repo_slug=f"{self._owner}/{self._repo}",
            job_name=job_name,
            run_id=run_id,
        )

        repo_slug = incident.repo_slug

        def _metrics_sink(rung: str, outcome: str) -> None:
            record_fix_ladder_outcome(repo_slug, rung, outcome)

        def _escalation_sink(repo: str, sig_hash: str) -> None:
            record_fix_ladder_escalation(repo, sig_hash)

        try:
            result = await run_fix_ladder(
                incident,
                sandbox=self._fix_ladder_sandbox,
                memory_retriever=self._memory_retriever,
                agent_name="self_heal_agent",
                max_rungs=self._fix_ladder_max_rungs,
                metrics_sink=_metrics_sink,
                escalation_metrics_sink=_escalation_sink,
            )
        except Exception as exc:  # noqa: BLE001 - ladder errors fall back to legacy
            logger.warning("Self-heal: fix ladder raised (%s) — falling back", exc)
            return None

        # Emit a summary metric for the top-level outcome so dashboards
        # can split "fixed via ladder" vs "escalated to LLM".
        _metrics_sink("__ladder__", result.outcome)

        # Publish the ``:Incident`` row regardless of outcome. Wave B3's
        # retriever needs the full corpus, not just the escalations.
        summary = (
            f"Self-heal {kind.value} in {job_name} — ladder {result.outcome} "
            f"({len(result.rungs_run)} rung(s))."
        )
        try:
            await publish_incident_with_embedding(
                IncidentMemory(
                    repo=repo_slug,
                    error_signature=sig,
                    kind=kind.value,
                    job_name=job_name,
                    summary=summary,
                    fix_outcome=result.outcome,
                    run_id=str(run_id) if run_id is not None else None,
                    rungs_tried=[r.name for r in result.rungs_run],
                ),
                embedder=self._memory_embedder,
                write_embeddings=self._write_embeddings,
            )
        except Exception as exc:  # noqa: BLE001 - graph writes never fail the dispatch
            logger.info("Self-heal: incident publish failed (%s)", exc)

        # PR open for ``fixed`` / ``partial`` outcomes.
        if result.outcome in {"fixed", "partial"}:
            try:
                opened = await open_fix_ladder_pr(
                    github=self._github,
                    owner=self._owner,
                    repo=self._repo,
                    base_branch=self._fix_ladder_base_branch,
                    sandbox_root=self._fix_ladder_sandbox.working_tree,
                    result=result,
                    error_signature=sig,
                    branch_prefix=self._fix_ladder_branch_prefix,
                    pr_label=self._fix_ladder_pr_label,
                )
            except Exception as exc:  # noqa: BLE001 - PR open is best-effort
                logger.warning("Self-heal: fix-ladder PR open failed (%s)", exc)
                opened = None
            if opened is not None:
                logger.info(
                    "Self-heal: fix-ladder PR #%d opened (outcome=%s)",
                    opened.number,
                    result.outcome,
                )

        return result

    async def _create_local_tracking_issue(
        self,
        job_name: str,
        title: str,
        upstream_issue: int,
        log_text: str,
        sig: str,
        *,
        run_id: int | None = None,
    ) -> Issue:
        full_title = f"🩺 Caretaker upstream bug filed: {title}"
        run_id_fragment = f" run_id:{run_id}" if run_id else ""
        body = (
            f"{SELF_HEAL_MARKER} sig:{sig}{run_id_fragment} -->\n\n"
            f"## Caretaker upstream bug filed\n\n"
            f"The self-heal agent detected a caretaker library bug in job `{job_name}` "
            f"and opened **ianlintner/caretaker#{upstream_issue}** upstream.\n\n"
            f"This issue tracks the local impact. It will be auto-closed when the upstream "
            f"fix is released and caretaker is upgraded.\n\n"
            f"<details><summary>Log snippet</summary>\n\n```\n{log_text[-2000:]}\n```\n\n</details>"
        )
        await self._ensure_label(SELF_HEAL_LABEL, "0075ca", "Caretaker self-heal: fix needed")
        return await self._issues.create(
            title=full_title,
            body=body,
            labels=[SELF_HEAL_LABEL],
        )

    async def _ensure_label(self, name: str, color: str, description: str) -> None:
        with contextlib.suppress(Exception):
            await self._github._post(
                f"/repos/{self._owner}/{self._repo}/labels",
                json={"name": name, "color": color, "description": description},
            )


# ── Failure classification ────────────────────────────────────────────────────


# Specific error patterns that indicate the tracking issue has hit GitHub's
# 2500-comment limit.  Classified before the generic INTEGRATION_ERROR path.
_TRACKING_ISSUE_FULL_PATTERNS = [
    re.compile(r"Commenting is disabled on issues with more than", re.IGNORECASE),
    re.compile(r"2500 comments", re.IGNORECASE),
]

_CONFIG_PATTERNS = [
    re.compile(r"pydantic|ValidationError|extra fields|field required", re.IGNORECASE),
    re.compile(r"ValueError.*[Cc]onfig", re.IGNORECASE),
    re.compile(r"yaml\.scanner\.ScannerError|yaml\.parser\.ParserError", re.IGNORECASE),
    re.compile(r"Config.*v1.*not supported|SUPPORTED_CONFIG_VERSIONS", re.IGNORECASE),
]

_INTEGRATION_PATTERNS = [
    re.compile(r"GITHUB_TOKEN.*required|401 Unauthorized|403 Forbidden", re.IGNORECASE),
    re.compile(r"ANTHROPIC_API_KEY|OPENAI_API_KEY", re.IGNORECASE),
    re.compile(r"GitHubAPIError", re.IGNORECASE),
    re.compile(r"httpx.*ConnectError|ConnectionRefused", re.IGNORECASE),
]

_UPSTREAM_BUG_PATTERNS = [
    re.compile(r"AttributeError|TypeError|IndexError|KeyError", re.IGNORECASE),
    re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(
        r"caretaker\.(orchestrator|pr_agent|issue_agent|upgrade_agent|devops_agent)\.",
        re.IGNORECASE,
    ),
    re.compile(r"unexpected keyword argument|takes \d+ positional argument", re.IGNORECASE),
]

_TRANSIENT_PATTERNS = [
    re.compile(r"rate limit|429|retry after", re.IGNORECASE),
    re.compile(r"timed out|timeout", re.IGNORECASE),
    re.compile(r"network.*error|connection.*reset", re.IGNORECASE),
    re.compile(r"secondary rate limit", re.IGNORECASE),
]


def _decode_job_log_payload(content: bytes, fallback_text: str) -> str:
    """Decode Actions job log payload from zip/gzip/plain text."""
    if content.startswith(b"PK\x03\x04"):
        with contextlib.suppress(Exception), zipfile.ZipFile(io.BytesIO(content)) as archive:
            parts = [
                archive.read(name).decode("utf-8", errors="replace")
                for name in archive.namelist()
                if not name.endswith("/")
            ]
            decoded = "\n".join(parts).strip()
            if decoded:
                return decoded

    if content.startswith(b"\x1f\x8b"):
        with contextlib.suppress(Exception):
            decoded = gzip.decompress(content).decode("utf-8", errors="replace").strip()
            if decoded:
                return decoded

    return fallback_text


# Markers that indicate GitHub Actions post-job cleanup has started.
# Everything after the first match is setup/teardown noise, not the real error.
_POST_CLEANUP_RE = re.compile(
    r"(?:Post job cleanup\.|Cleaning up orphan processes\.|##\[section\]Finishing Job)",
    re.IGNORECASE,
)


def _pre_cleanup_log(log_text: str, max_chars: int = 8000) -> str:
    """Return the log up to (but not including) post-job cleanup lines.

    GitHub Actions appends cleanup steps after every job (``Post job cleanup``,
    ``Cleaning up orphan processes``).  These lines dominate the tail and
    caused the classifier to always fall through to UNKNOWN because the real
    error patterns only appear earlier in the log.
    """
    match = _POST_CLEANUP_RE.search(log_text)
    pre_cleanup = log_text[: match.start()] if match else log_text
    return pre_cleanup[-max_chars:]


def _classify_failure(job_name: str, log_text: str) -> tuple[FailureKind, str, str]:
    """Return (kind, short title, description) from a job log."""
    log_tail = _pre_cleanup_log(log_text)

    if any(p.search(log_tail) for p in _TRANSIENT_PATTERNS):
        return (
            FailureKind.TRANSIENT,
            f"Transient failure in {job_name}",
            "Rate limit or network timeout — no action needed.",
        )

    if any(p.search(log_tail) for p in _TRACKING_ISSUE_FULL_PATTERNS):
        return (
            FailureKind.CONFIG_ERROR,
            "Tracking issue has reached GitHub's comment limit",
            "The caretaker orchestrator tracking issue has accumulated more than "
            "2500 comments and GitHub has disabled further commenting.\n\n"
            "The caretaker library will automatically close the full issue and "
            "create a replacement on the next run.  No manual action is needed "
            "once this fix is deployed.",
        )

    if any(p.search(log_tail) for p in _CONFIG_PATTERNS):
        # Extract the specific error message
        msg = _extract_first_error(log_tail)
        return (
            FailureKind.CONFIG_ERROR,
            f"Config error in caretaker: {msg[:80]}",
            f"A configuration validation error caused the caretaker run to fail.\n\nError: {msg}",
        )

    if any(p.search(log_tail) for p in _INTEGRATION_PATTERNS):
        msg = _extract_first_error(log_tail)
        return (
            FailureKind.INTEGRATION_ERROR,
            f"Integration/auth error in caretaker: {msg[:80]}",
            f"An API integration or authentication error caused the caretaker run to fail.\n\n"
            f"Error: {msg}\n\n"
            f"Common fixes:\n"
            f"- Verify `GITHUB_TOKEN` has the required permissions\n"
            f"- Verify `COPILOT_PAT` is set if caretaker assigns issues to Copilot via the API\n"
            f"- Verify `ANTHROPIC_API_KEY` is set if LLM features are enabled\n"
            f"- Check that repository secrets are configured correctly",
        )

    if any(p.search(log_tail) for p in _UPSTREAM_BUG_PATTERNS):
        msg = _extract_first_error(log_tail)
        return (
            FailureKind.UPSTREAM_BUG,
            f"Caretaker library error: {msg[:80]}",
            f"An unhandled exception in the caretaker library caused the run to fail.\n\n"
            f"Error: {msg}",
        )

    msg = _extract_first_error(log_text)
    return (
        FailureKind.UNKNOWN,
        f"Unknown caretaker failure: {msg[:80]}",
        f"The caretaker workflow failed with an unclassified error.\n\nError: {msg}",
    )


def _extract_first_error(log_text: str) -> str:
    """Return the first non-trivial error-looking line from the log.

    Extraction order:
    1. GitHub Actions ``##[error]`` annotations (strongest signal)
    2. Non-zero ``Process completed with exit code N`` lines
    3. Generic keyword scan

    Returned lines are normalized by removing leading timestamps and
    ``##[error]`` marker noise.
    """
    lines = log_text.splitlines()

    def _normalize_error_line(line: str) -> str:
        normalized = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", line).strip()
        normalized = normalized.replace("##[error]", "", 1).strip()
        return normalized[:300]

    # Pass 1: prefer ##[error] annotations — these are GitHub Actions' own
    # markers and almost always describe the real failure.
    for line in lines:
        stripped = line.strip()
        if "##[error]" in stripped and len(stripped) > 20:
            return _normalize_error_line(stripped)

    # Pass 2: common Actions failure line when annotation markers are absent.
    # Ignore exit code 0 so successful step lines are not treated as failures.
    for line in lines:
        stripped = line.strip()
        match = re.search(
            r"process completed with exit code\s+(\d+)",
            stripped,
            flags=re.IGNORECASE,
        )
        if match and int(match.group(1)) != 0:
            return _normalize_error_line(stripped)

    # Pass 3: generic keyword scan
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 20 and any(
            kw in stripped
            for kw in ("Error", "error", "Exception", "FAILED", "invalid", "required")
        ):
            return _normalize_error_line(stripped)

    return log_text.strip()[:200]


def _sig(job_name: str, kind: FailureKind, title: str) -> str:
    import hashlib

    raw = f"{job_name}:{kind.value}:{title[:60]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


_SIG_PATTERN = re.compile(r"sig:([a-f0-9]{12})")


def _extract_sig_from_body(body: str) -> str | None:
    """Return the ``sig:xxxxxxxxxxxx`` signature embedded in an issue body.

    Returns ``None`` when the body carries no marker — older issues from
    before the marker was introduced are treated as anonymous and simply
    don't participate in the per-sig storm-cap histogram.
    """
    match = _SIG_PATTERN.search(body)
    return match.group(1) if match else None


def _build_fix_issue_body(
    job_name: str,
    kind: FailureKind,
    title: str,
    details: str,
    log_text: str,
    sig: str,
    *,
    run_id: int | None = None,
    ladder_escalation: str | None = None,
) -> str:
    kind_label = {
        FailureKind.CONFIG_ERROR: "⚙️ Config error",
        FailureKind.INTEGRATION_ERROR: "🔌 Integration / auth error",
        FailureKind.UNKNOWN: "❓ Unknown error",
    }.get(kind, "🩺 Error")

    run_id_fragment = f" run_id:{run_id}" if run_id else ""
    causal = make_causal_marker("self-heal", run_id=run_id)
    # Wave A3: when the fix ladder escalated (or produced a partial
    # fix that still needs follow-up), surface its verdict in the
    # issue body so the Copilot assignee sees what the deterministic
    # pass already tried.
    ladder_block = ""
    if ladder_escalation:
        ladder_block = f"\n---\n\n## Fix-ladder verdict\n\n{ladder_escalation.rstrip()}\n\n"
    return (
        f"{SELF_HEAL_MARKER} sig:{sig}{run_id_fragment} -->\n"
        f"{causal}\n\n"
        f"## {kind_label}\n\n"
        f"{details}\n\n"
        f"**Job:** `{job_name}`\n\n"
        f"<details><summary>Log snippet</summary>\n\n"
        f"```\n{log_text[-3000:]}\n```\n\n</details>\n\n"
        f"{ladder_block}"
        f"---\n\n"
        f"<!-- caretaker:self-heal-assignment -->\n"
        f"TYPE: BUG_SIMPLE\n"
        f"KIND: {kind.value}\n\n"
        f"**Root cause:**\n{details}\n\n"
        f"**Acceptance criteria:**\n"
        f"- [ ] Caretaker workflow runs successfully on this repo\n"
        f"- [ ] The root cause identified above is resolved\n"
        f"- [ ] Add a test or config guard to prevent regression\n"
        f"<!-- /caretaker:self-heal-assignment -->\n"
    )
