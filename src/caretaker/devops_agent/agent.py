"""DevOps Agent — detects CI failures on the default branch and files fix issues."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from caretaker.devops_agent.log_analyzer import FailureSummary, analyze_job_log
from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue

logger = logging.getLogger(__name__)

# Label applied to issues opened by this agent
BUILD_FAILURE_LABEL = "devops:build-failure"
DEVOPS_AGENT_MARKER = "<!-- caretaker:devops-build-failure"


@dataclass
class DevOpsReport:
    """Results from a single DevOps agent run."""

    failures_detected: int = 0
    issues_created: list[int] = field(default_factory=list)
    issues_skipped: int = 0  # duplicate detection
    errors: list[str] = field(default_factory=list)


class DevOpsAgent:
    """Monitors CI runs on the default branch and creates Copilot-assigned fix issues."""

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        default_branch: str = "main",
        max_issues_per_run: int = 3,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._default_branch = default_branch
        self._max_issues_per_run = max_issues_per_run
        self._issues = GitHubIssueTools(github, owner, repo)

    async def run(self, event_payload: dict[str, Any] | None = None) -> DevOpsReport:
        """Inspect recent CI runs on the default branch and act on failures."""
        report = DevOpsReport()

        try:
            failing_jobs = await self._discover_failing_jobs(event_payload)
        except Exception as e:
            logger.error("DevOps agent: failed to discover failing jobs: %s", e)
            report.errors.append(str(e))
            return report

        if not failing_jobs:
            logger.info("DevOps agent: no failing CI jobs on %s", self._default_branch)
            return report

        report.failures_detected = len(failing_jobs)
        logger.info("DevOps agent: %d failing job(s) found", len(failing_jobs))

        # Fetch existing open devops issues to avoid duplicates
        existing_signatures = await self._get_existing_failure_signatures()

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

            try:
                issue = await self._create_fix_issue(summary, sig)
                report.issues_created.append(issue.number)
                created += 1
                logger.info(
                    "DevOps agent: created fix issue #%d for job '%s'",
                    issue.number,
                    summary.job_name,
                )
            except Exception as e:
                logger.error("DevOps agent: failed to create issue: %s", e)
                report.errors.append(str(e))

        return report

    # ── Private helpers ─────────────────────────────────────────────────────

    async def _discover_failing_jobs(
        self, event_payload: dict[str, Any] | None
    ) -> list[FailureSummary]:
        """Return FailureSummary objects for each failed CI job on the default branch."""
        summaries: list[FailureSummary] = []

        # If triggered by a workflow_run event, use its data directly
        if event_payload and event_payload.get("workflow_run"):
            run = event_payload["workflow_run"]
            if run.get("conclusion") not in ("failure", "timed_out"):
                return []
            if run.get("head_branch") != self._default_branch:
                return []

            run_id = run["id"]
            jobs = await self._get_failed_jobs_for_run(run_id)
            for job in jobs:
                log = await self._fetch_job_log(job["id"])
                summaries.append(
                    analyze_job_log(job["name"], job.get("conclusion", "failure"), log)
                )
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
            summaries.append(analyze_job_log(cr.name, cr.conclusion or "failure", log_text))

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

    async def _create_fix_issue(self, summary: FailureSummary, sig: str) -> Issue:
        """Create a GitHub issue and assign to @copilot for fix."""
        title = (
            f"🔧 CI failure on `{self._default_branch}`: {summary.job_name} ({summary.category})"
        )

        body = _build_issue_body(summary, sig, self._default_branch)

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


def _build_issue_body(summary: FailureSummary, sig: str, branch: str) -> str:
    return f"""{DEVOPS_AGENT_MARKER} sig:{sig} -->

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
