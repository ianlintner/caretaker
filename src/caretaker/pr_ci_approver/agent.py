"""Agent that unsticks bot-triggered GitHub Actions runs.

Rationale — every bot-generated PR (Copilot, Dependabot, etc.) triggers
workflow runs that GitHub leaves in ``conclusion=action_required`` until
a human approves them via the Actions UI. There is no GitHub REST
endpoint that covers this case for same-repo branches (the only
``/approve`` route is fork-PR-only). When caretaker sits in a
review-revise-merge loop with a bot, those stuck runs mean the PR
never hits GREEN and the merge loop silently stalls.

This agent:
- enumerates recent workflow runs with ``status=action_required``,
- filters to whitelisted bot actors and relevant event types,
- tries the ``POST /actions/runs/{id}/approve`` endpoint when
  ``auto_approve=true`` (graceful on the expected 403/422 path),
- always *surfaces* stuck-run counts in the RunSummary so operators
  can see the backlog from the digest.

It is intentionally read-oriented by default: ``auto_approve`` is
False so an opt-in step is required to side-effect on GitHub.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.github_client.api import GitHubAPIError

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


@dataclass
class _StuckRunInfo:
    """Compact record of a run we considered for unsticking."""

    run_id: int
    workflow_name: str
    actor: str
    event: str
    head_branch: str
    created_at: str
    approved: bool = False
    approval_error: str | None = None


@dataclass
class PRCIApproverReport:
    """Outcome of a single agent execution."""

    runs_stuck: int = 0
    runs_approved: int = 0
    runs_surfaced: int = 0  # stuck runs we logged but didn't auto-approve
    details: list[_StuckRunInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _run_timestamp_iso(run: dict[str, Any]) -> str:
    ts = run.get("created_at") or run.get("run_started_at") or ""
    return str(ts)


def _run_age_hours(run: dict[str, Any], now: datetime) -> float:
    raw = _run_timestamp_iso(run)
    if not raw:
        return 0.0
    try:
        # Normalise the GitHub trailing-Z form to a tz-aware datetime.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).total_seconds() / 3600.0


def _run_actor(run: dict[str, Any]) -> str:
    """Return the most specific actor login we can read from the run.

    GitHub's API exposes both ``actor`` (the account that originated the
    push/comment) and ``triggering_actor`` (the account that actually
    caused this run to start — usually the same but may differ when a
    user acts on behalf of a bot). We match on either so the whitelist
    can be tight but still catch both shapes.
    """
    for key in ("actor", "triggering_actor"):
        payload = run.get(key)
        if isinstance(payload, dict):
            login = payload.get("login")
            if isinstance(login, str) and login:
                return login
    return ""


class PRCIApproverAgent(BaseAgent):
    """BaseAgent implementation — no separate adapter needed."""

    @property
    def name(self) -> str:
        return "pr-ci-approver"

    def enabled(self) -> bool:
        return self._ctx.config.pr_ci_approver.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.pr_ci_approver
        report = PRCIApproverReport()
        allowed = {a.lower() for a in cfg.allowed_actors}
        trigger_events = set(cfg.trigger_events)
        now = datetime.now(UTC)

        try:
            runs = await self._ctx.github.list_workflow_runs(
                self._ctx.owner,
                self._ctx.repo,
                status="action_required",
                per_page=cfg.max_runs_per_run,
            )
        except GitHubAPIError as exc:
            logger.warning("pr_ci_approver: failed to list workflow runs: %s", exc)
            report.errors.append(f"list_workflow_runs failed: {exc}")
            return self._make_result(report)

        for run in runs:
            if not isinstance(run, dict):
                continue
            actor = _run_actor(run)
            event = str(run.get("event") or "")
            # Whitelist gate — both actor and event must match. We lower-case
            # the actor because GitHub spells some bots 'Copilot' and others
            # 'copilot-swe-agent[bot]' and operators frequently copy names
            # inconsistently into their config.
            if actor.lower() not in allowed:
                continue
            if trigger_events and event not in trigger_events:
                continue
            # Skip ancient runs: if a newer push superseded them we should
            # not silently approve a stale SHA.
            age_hours = _run_age_hours(run, now)
            if age_hours > cfg.max_age_hours:
                continue

            run_id = int(run.get("id") or 0)
            if not run_id:
                continue

            info = _StuckRunInfo(
                run_id=run_id,
                workflow_name=str(run.get("name") or run.get("workflow_name") or ""),
                actor=actor,
                event=event,
                head_branch=str(run.get("head_branch") or ""),
                created_at=_run_timestamp_iso(run),
            )
            report.runs_stuck += 1

            if cfg.auto_approve and not self._ctx.dry_run:
                try:
                    ok = await self._ctx.github.approve_workflow_run(
                        self._ctx.owner,
                        self._ctx.repo,
                        run_id,
                    )
                    info.approved = ok
                    if ok:
                        report.runs_approved += 1
                        logger.info(
                            "pr_ci_approver: approved run %d (workflow=%s actor=%s)",
                            run_id,
                            info.workflow_name,
                            actor,
                        )
                    else:
                        info.approval_error = "approve endpoint returned False"
                        report.runs_surfaced += 1
                except GitHubAPIError as exc:
                    # The expected failure class: same-repo bot PRs return
                    # 403 "This run is not from a fork pull request". We log
                    # this at DEBUG so we don't spam on every tick, but we
                    # still count the run as surfaced so operators see it.
                    info.approval_error = f"{exc.status_code}: {exc}"
                    report.runs_surfaced += 1
                    logger.debug(
                        "pr_ci_approver: approve failed for run %d: %s",
                        run_id,
                        exc,
                    )
            else:
                report.runs_surfaced += 1

            report.details.append(info)

        return self._make_result(report)

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.ci_runs_stuck = int(result.extra.get("runs_stuck", 0))
        summary.ci_runs_approved = int(result.extra.get("runs_approved", 0))
        summary.ci_runs_surfaced = int(result.extra.get("runs_surfaced", 0))

    def _make_result(self, report: PRCIApproverReport) -> AgentResult:
        actions: list[str] = []
        for info in report.details:
            suffix = "approved" if info.approved else "surfaced"
            actions.append(
                f"run={info.run_id} workflow={info.workflow_name} "
                f"actor={info.actor} event={info.event} → {suffix}"
            )
        return AgentResult(
            processed=report.runs_stuck,
            actions=actions,
            errors=report.errors,
            extra={
                "runs_stuck": report.runs_stuck,
                "runs_approved": report.runs_approved,
                "runs_surfaced": report.runs_surfaced,
                "details": [
                    {
                        "run_id": d.run_id,
                        "workflow_name": d.workflow_name,
                        "actor": d.actor,
                        "event": d.event,
                        "head_branch": d.head_branch,
                        "approved": d.approved,
                        "approval_error": d.approval_error,
                    }
                    for d in report.details
                ],
            },
        )
