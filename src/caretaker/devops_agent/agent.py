"""DevOps Agent — detects CI failures on the default branch and files fix issues."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.causal import make_causal_marker
from caretaker.devops_agent.log_analyzer import FailureSummary, analyze_job_log
from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue

logger = logging.getLogger(__name__)

# Label applied to issues opened by this agent
BUILD_FAILURE_LABEL = "devops:build-failure"
DEVOPS_AGENT_MARKER = "<!-- caretaker:devops-build-failure"
# Marker stamped onto PR comments when we route a fix request onto an
# open PR instead of opening a parallel build-failure issue. Same family
# as ``DEVOPS_AGENT_MARKER`` so future cleanup / dedup passes can find
# both via the ``caretaker:devops-build-failure`` substring.
DEVOPS_PR_COMMENT_MARKER = "<!-- caretaker:devops-build-failure-on-pr"


@dataclass
class DevOpsReport:
    """Results from a single DevOps agent run."""

    failures_detected: int = 0
    issues_created: list[int] = field(default_factory=list)
    issues_skipped: int = 0  # duplicate detection
    issues_closed_resolved: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Sigs actioned this run — used to update persisted state dedup
    actioned_sigs: list[str] = field(default_factory=list)
    # Updated cooldown map to persist back to state
    updated_cooldowns: dict[str, str] = field(default_factory=dict)
    # PRs we routed a CI fix comment onto instead of opening a parallel
    # build-failure issue. Populated when the failing commit is the head
    # of an open PR — keeps the work where the reviewer / coding agent
    # is already engaged.
    pr_comments_posted: list[int] = field(default_factory=list)


class DevOpsAgent:
    """Monitors CI runs on the default branch and creates Copilot-assigned fix issues."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        default_branch: str = "main",
        max_issues_per_run: int = 3,
        known_sigs: set[str] | None = None,
        cooldown_hours: int = 6,
        issue_cooldowns: dict[str, str] | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._default_branch = default_branch
        self._max_issues_per_run = max_issues_per_run
        self._issues = GitHubIssueTools(github, owner, repo)
        # Pre-seeded sigs from persisted state (survive issue close/reopen cycles)
        self._known_sigs: set[str] = set(known_sigs or [])
        self._cooldown_hours = cooldown_hours
        # Mutable copy — callers read back updated_cooldowns after run()
        self._issue_cooldowns: dict[str, str] = dict(issue_cooldowns or {})

    async def run(self, event_payload: dict[str, Any] | None = None) -> DevOpsReport:
        """Inspect recent CI runs on the default branch and act on failures."""
        report = DevOpsReport()

        # Extract workflow run_id for grouping related issues
        run_id: int | None = None
        if event_payload and event_payload.get("workflow_run"):
            run_id = event_payload["workflow_run"].get("id")

        try:
            failing_jobs = await self._discover_failing_jobs(event_payload)
        except Exception as e:
            logger.error("DevOps agent: failed to discover failing jobs: %s", e)
            report.errors.append(str(e))
            return report

        if not failing_jobs:
            logger.info("DevOps agent: no failing CI jobs on %s", self._default_branch)
            # Sweep open build-failure issues — anything still open
            # corresponds to a failure that no longer reproduces. Close
            # them so `maintainer:action-required` clears without manual
            # housekeeping.
            try:
                report.issues_closed_resolved = await self._close_resolved_failure_issues(
                    active_sigs=set()
                )
            except Exception as e:
                logger.warning("DevOps agent: resolved-failure close pass failed: %s", e)
            return report

        report.failures_detected = len(failing_jobs)
        logger.info("DevOps agent: %d failing job(s) found", len(failing_jobs))

        # Fetch existing open devops issues to avoid duplicates
        existing_signatures = await self._get_existing_failure_signatures()
        # Merge open-issue sigs with pre-seeded state sigs for robust dedup
        existing_signatures |= self._known_sigs

        # Cross-agent run_id dedup: if a self-heal issue already exists for
        # this workflow run, skip creating devops issues entirely.
        if run_id and await self._run_id_already_tracked(run_id):
            logger.info(
                "DevOps agent: run_id %d already tracked by another agent, skipping", run_id
            )
            report.issues_skipped = len(failing_jobs)
            return report

        created = 0
        for summary in failing_jobs:
            if created >= self._max_issues_per_run:
                logger.info("DevOps agent: max issues/run reached, stopping")
                break

            sig = _failure_signature(summary)
            if sig in existing_signatures:
                logger.debug("DevOps agent: duplicate issue for %s, skipping", summary.job_name)
                report.issues_skipped += 1
                continue

            # Cooldown: same job+category recently actioned → skip even with a different sig
            coarse_key = f"devops:{summary.job_name}:{summary.category}"
            if self._is_on_cooldown(coarse_key):
                logger.info("DevOps agent: cooldown active for %s, skipping", coarse_key)
                report.issues_skipped += 1
                continue

            # Prefer routing the fix request onto an open PR whose head is
            # this failing commit — keeps the @copilot work where the
            # reviewer is already engaged. Only fall through to a fresh
            # issue when no such PR exists (or the lookup fails).
            try:
                pr_number = await self._find_open_pr_for_failure(summary)
            except Exception as e:
                logger.warning(
                    "DevOps agent: PR-by-SHA lookup failed for %s: %s",
                    summary.job_name,
                    e,
                )
                pr_number = None

            if pr_number is not None:
                try:
                    posted = await self._post_pr_fix_comment(pr_number, summary, sig, run_id=run_id)
                except Exception as e:
                    logger.error(
                        "DevOps agent: failed to comment on PR #%d: %s",
                        pr_number,
                        e,
                    )
                    report.errors.append(str(e))
                    continue

                if posted:
                    report.pr_comments_posted.append(pr_number)
                    report.actioned_sigs.append(sig)
                    self._record_cooldown(coarse_key)
                    created += 1
                    logger.info(
                        "DevOps agent: routed fix request to PR #%d "
                        "(job '%s', sig %s) instead of opening an issue",
                        pr_number,
                        summary.job_name,
                        sig,
                    )
                else:
                    # An identical comment was already on the PR — count as
                    # a dedup skip so the report numbers stay honest.
                    report.issues_skipped += 1
                continue

            try:
                issue = await self._create_fix_issue(summary, sig, run_id=run_id)
                report.issues_created.append(issue.number)
                report.actioned_sigs.append(sig)
                self._record_cooldown(coarse_key)
                created += 1
                logger.info(
                    "DevOps agent: created fix issue #%d for job '%s'",
                    issue.number,
                    summary.job_name,
                )
            except Exception as e:
                logger.error("DevOps agent: failed to create issue: %s", e)
                report.errors.append(str(e))

        # Close any open build-failure issues whose signature is no longer
        # reproducing this run. Subset of failing_jobs may have been fixed
        # in main while others remain — close the resolved ones.
        active_sigs = {_failure_signature(s) for s in failing_jobs}
        try:
            report.issues_closed_resolved = await self._close_resolved_failure_issues(
                active_sigs=active_sigs
            )
        except Exception as e:
            logger.warning("DevOps agent: resolved-failure close pass failed: %s", e)

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

    async def _discover_failing_jobs(
        self, event_payload: dict[str, Any] | None
    ) -> list[FailureSummary]:
        """Return FailureSummary objects for each failed CI job on the default branch.

        Each summary carries ``head_sha`` populated from the workflow_run
        event (or the default branch HEAD in the fallback path) so the
        caller can route a fix request onto the owning PR when one
        exists.
        """
        summaries: list[FailureSummary] = []

        # If triggered by a workflow_run event, use its data directly
        if event_payload and event_payload.get("workflow_run"):
            run = event_payload["workflow_run"]
            if run.get("conclusion") not in ("failure", "timed_out"):
                return []
            if run.get("head_branch") != self._default_branch:
                return []

            run_id = run["id"]
            head_sha = run.get("head_sha") or run.get("head_commit", {}).get("id")
            jobs = await self._get_failed_jobs_for_run(run_id)
            for job in jobs:
                log = await self._fetch_job_log(job["id"])
                summary = analyze_job_log(job["name"], job.get("conclusion", "failure"), log)
                summary.head_sha = head_sha
                summaries.append(summary)
            return summaries

        # Fallback: inspect the latest check-runs on the default branch HEAD
        branch_info = await self._github._get(
            f"/repos/{self._owner}/{self._repo}/branches/{self._default_branch}"
        )
        if not branch_info:
            return []

        sha = branch_info["commit"]["sha"]
        check_runs = await self._github.get_check_runs(self._owner, self._repo, sha)

        failed = [cr for cr in check_runs if cr.conclusion in ("failure", "timed_out")]
        for cr in failed:
            # We don't have full logs via check-run endpoint, build from output fields
            log_text = "\n".join(filter(None, [cr.output_title, cr.output_summary]))
            summary = analyze_job_log(cr.name, cr.conclusion or "failure", log_text)
            summary.head_sha = sha
            summaries.append(summary)

        return summaries

    async def _get_failed_jobs_for_run(self, run_id: int) -> list[dict[str, Any]]:
        data = await self._github._get(
            f"/repos/{self._owner}/{self._repo}/actions/runs/{run_id}/jobs"
        )
        if not data:
            return []
        return [j for j in data.get("jobs", []) if j.get("conclusion") in ("failure", "timed_out")]

    async def _fetch_job_log(self, job_id: int) -> str:
        """Download the text log for a specific Actions job (best-effort)."""
        try:
            # The API returns a redirect to a zip; we read the raw text log
            resp = await self._github._client.get(
                f"/repos/{self._owner}/{self._repo}/actions/jobs/{job_id}/logs",
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            logger.debug("Could not fetch job log %s: %s", job_id, e)
        return ""

    async def _get_existing_failure_signatures(self) -> set[str]:
        """Return the set of failure signatures already tracked in open issues."""
        issues = await self._issues.list(state="open", labels=BUILD_FAILURE_LABEL)
        sigs: set[str] = set()
        for issue in issues:
            if DEVOPS_AGENT_MARKER in issue.body:
                # Extract the sig embedded in the marker
                for line in issue.body.splitlines():
                    if line.startswith(DEVOPS_AGENT_MARKER):
                        match = re.search(r"\bsig:([0-9a-f]+)\b", line)
                        if match:
                            sigs.add(match.group(1))
        return sigs

    async def _run_id_already_tracked(self, run_id: int) -> bool:
        """Check if any open issue (any agent) already references this run_id."""
        return await self._issues.run_id_tracked(
            run_id, [BUILD_FAILURE_LABEL, "caretaker:self-heal"]
        )

    async def _find_open_pr_for_failure(self, summary: FailureSummary) -> int | None:
        """Return the number of an open PR whose head is the failing commit.

        Returns ``None`` when no SHA is known, when the lookup yields no
        open PRs, or when more than one open PR shares the SHA (rare —
        we prefer to fall through to the issue path rather than guess
        which PR to comment on).
        """
        sha = summary.head_sha
        if not sha:
            return None
        prs = await self._github.find_open_pull_requests_for_sha(self._owner, self._repo, sha)
        # Match strictly by head_sha so a PR that merely *contains* the
        # commit elsewhere in its history (e.g. a long-running branch
        # that ate the commit via merge) does not get pinged. Only the
        # PR whose HEAD is currently this commit gets the @copilot task.
        head_match = [pr for pr in prs if pr.head_sha == sha]
        if len(head_match) != 1:
            return None
        return head_match[0].number

    async def _post_pr_fix_comment(
        self,
        pr_number: int,
        summary: FailureSummary,
        sig: str,
        *,
        run_id: int | None = None,
    ) -> bool:
        """Post a ``@copilot`` fix request onto the failing commit's PR.

        Returns ``True`` when a new comment was posted, ``False`` when an
        existing devops marker for the same signature was already on the
        PR (dedup — we don't pile duplicate task comments).
        """
        existing_comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        for comment in existing_comments:
            if DEVOPS_PR_COMMENT_MARKER in (comment.body or "") and f"sig:{sig}" in (
                comment.body or ""
            ):
                logger.info(
                    "DevOps agent: PR #%d already has devops fix comment "
                    "for sig %s — skipping duplicate",
                    pr_number,
                    sig,
                )
                return False

        body = _build_pr_comment_body(summary, sig, run_id=run_id)
        await self._issues.comment(pr_number, body)
        return True

    async def _create_fix_issue(
        self, summary: FailureSummary, sig: str, *, run_id: int | None = None
    ) -> Issue:
        """Create a GitHub issue and assign to @copilot for fix."""
        title = (
            f"🔧 CI failure on `{self._default_branch}`: {summary.job_name} ({summary.category})"
        )

        body = _build_issue_body(summary, sig, self._default_branch, run_id=run_id)

        # Ensure the label exists
        await self._issues.ensure_label(
            BUILD_FAILURE_LABEL, "d93f0b", "CI build failure on default branch"
        )

        return await self._issues.create(
            title=title,
            body=body,
            labels=[BUILD_FAILURE_LABEL, "bug"],
            assignees=["copilot"],
            copilot_assignment=self._issues.default_copilot_assignment(
                base_branch=self._default_branch,
            ),
        )

    async def _close_resolved_failure_issues(self, *, active_sigs: set[str]) -> list[int]:
        """Close open build-failure issues whose signature is no longer firing.

        Surfaced live on caretaker-qa#51: an old transient
        ``maintain (unknown)`` failure was tracked, the underlying
        problem (broken bootstrap-check) was fixed via caretaker-qa#50
        + #54, every subsequent run had ``no failing CI jobs on main``
        — but the issue stayed ``OPEN`` with ``bug, devops:build-failure``
        labels because devops_agent had no resolved-close path.

        ``active_sigs`` is the set of failure signatures still firing on
        the current run. Issues whose embedded ``sig:<hex>`` is NOT in
        that set are considered resolved and closed (with a comment).

        Returns the list of issue numbers closed.
        """
        try:
            issues = await self._issues.list(state="open", labels=BUILD_FAILURE_LABEL)
        except Exception as e:
            logger.warning("Failed to list open build-failure issues: %s", e)
            return []

        closed: list[int] = []
        for issue in issues:
            if DEVOPS_AGENT_MARKER not in (issue.body or ""):
                continue
            sig: str | None = None
            for line in (issue.body or "").splitlines():
                if line.startswith(DEVOPS_AGENT_MARKER):
                    match = re.search(r"\bsig:([0-9a-f]+)\b", line)
                    if match:
                        sig = match.group(1)
                        break
            if sig is None or sig in active_sigs:
                continue
            logger.info(
                "Closing resolved build-failure issue #%d (sig %s no longer firing on %s)",
                issue.number,
                sig,
                self._default_branch,
            )
            try:
                await self._issues.comment(
                    issue.number,
                    f"Closed automatically: caretaker observed no failing CI jobs "
                    f"on `{self._default_branch}` matching this signature on the "
                    f"most recent run, so the build failure is considered "
                    f"resolved. Caretaker will re-open a fresh issue if the "
                    f"failure reappears.",
                )
                await self._issues.update(issue.number, state="closed")
                closed.append(issue.number)
            except Exception as e:
                logger.warning(
                    "Failed to close resolved build-failure issue #%d: %s", issue.number, e
                )
        return closed

    async def _ensure_label(self, name: str, color: str, description: str) -> None:
        """Create the label if it does not already exist (best-effort)."""
        with contextlib.suppress(Exception):
            await self._github._post(
                f"/repos/{self._owner}/{self._repo}/labels",
                json={"name": name, "color": color, "description": description},
            )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _failure_signature(summary: FailureSummary) -> str:
    """Stable short hash that identifies a unique failure (job + category)."""
    raw = f"{summary.job_name}:{summary.category}:{':'.join(summary.suspected_files[:3])}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _build_pr_comment_body(
    summary: FailureSummary,
    sig: str,
    *,
    run_id: int | None = None,
    parent_id: str | None = None,
) -> str:
    """Build the @copilot fix-request comment posted onto an owning PR.

    Mirrors the structure of :func:`_build_issue_body` so the reviewer /
    coding agent gets the same triage info, but framed as a PR comment
    ("fix this PR before merge") rather than a fresh tracking issue.
    """
    run_id_fragment = f" run_id:{run_id}" if run_id else ""
    causal = make_causal_marker("devops", run_id=run_id, parent=parent_id)
    return f"""{DEVOPS_PR_COMMENT_MARKER} sig:{sig}{run_id_fragment} -->
{causal}

@copilot

## CI failure on this PR's head commit

{summary.to_markdown()}

---

<!-- caretaker:devops-pr-task -->
TYPE: BUG_SIMPLE
CATEGORY: {summary.category}
JOB: {summary.job_name}

**What happened:**
The `{summary.job_name}` CI job failed on this PR's head commit
(category: **{summary.category}**). Caretaker is routing the fix request
here instead of opening a parallel build-failure issue so the work
stays attached to the PR you're already reviewing.

**Suspected files:**
{chr(10).join(f"- `{f}`" for f in summary.suspected_files) or "- _not identified — see log_"}

**Acceptance criteria:**
- [ ] The `{summary.job_name}` CI job passes on this PR
- [ ] No regressions in the rest of the test suite
- [ ] Push the fix to this PR (do NOT open a new PR)

**Instructions:**
1. Review the log snippet and error lines above
2. Identify the root cause in the suspected files
3. Apply the minimal fix needed
4. Add or update tests if a test is failing
5. Push to this branch — caretaker will pick up the next CI run automatically
<!-- /caretaker:devops-pr-task -->
"""


def _build_issue_body(
    summary: FailureSummary,
    sig: str,
    branch: str,
    *,
    run_id: int | None = None,
    parent_id: str | None = None,
) -> str:
    run_id_fragment = f" run_id:{run_id}" if run_id else ""
    causal = make_causal_marker("devops", run_id=run_id, parent=parent_id)
    return f"""{DEVOPS_AGENT_MARKER} sig:{sig}{run_id_fragment} -->
{causal}

## CI Build Failure — `{branch}` branch

{summary.to_markdown()}

---

<!-- caretaker:devops-assignment -->
TYPE: BUG_SIMPLE
BRANCH: {branch}
CATEGORY: {summary.category}

**Root cause (auto-analyzed):**
The `{summary.job_name}` CI job on the `{branch}` branch is failing.
Category: **{summary.category}**.

**Suspected files:**
{chr(10).join(f"- `{f}`" for f in summary.suspected_files) or "- _not identified — see log_"}

**Acceptance criteria:**
- [ ] The `{summary.job_name}` CI job passes on `{branch}`
- [ ] No regressions in the test suite
- [ ] PR references this issue (`Fixes #{"{issue_number}"}`)

**Instructions for Copilot:**
1. Review the log snippet and error lines above
2. Identify the root cause in the suspected files
3. Apply the minimal fix needed
4. Add or update tests if a test is failing
5. Open a PR targeting `{branch}` referencing this issue
<!-- /caretaker:devops-assignment -->
"""
