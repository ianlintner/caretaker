"""Escalation Agent — consolidates items needing human attention into a weekly digest issue."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

ESCALATION_DIGEST_LABEL = "maintainer:escalation-digest"
ESCALATION_AGENT_MARKER = "<!-- caretaker:escalation-digest"

# Labels that tag items requiring human intervention
_ACTION_LABELS = {
    "dependencies:major-upgrade": "Major dependency upgrade",
    "security:finding": "Security finding",
    "devops:build-failure": "CI build failure",
    "caretaker:self-heal": "Caretaker self-heal issue",
    "maintainer:escalated": "Escalated / stale work item",
    "help wanted": "Needs community/maintainer help",
}


@dataclass
class EscalationReport:
    items_found: int = 0
    digest_issue_number: int | None = None
    errors: list[str] = field(default_factory=list)


class EscalationAgent:
    """
    Scans open issues across all managed labels and produces a weekly
    'Human Action Required' digest issue for the repository maintainer.

    The digest is idempotent — it updates the existing open digest rather
    than creating duplicates.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        notify_assignees: list[str] | None = None,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._notify_assignees = notify_assignees or []
        self._issues = GitHubIssueTools(github, owner, repo)

    async def run(self) -> EscalationReport:
        report = EscalationReport()

        # Collect all items requiring action grouped by label
        buckets: dict[str, list[Any]] = {}
        for label, _description in _ACTION_LABELS.items():
            try:
                items = await self._issues.list(state="open", labels=label)
                if items:
                    buckets[label] = items
            except Exception as e:
                logger.warning("Escalation agent: could not list label '%s': %s", label, e)
                report.errors.append(f"{label}: {e}")

        total = sum(len(v) for v in buckets.values())
        report.items_found = total
        logger.info("Escalation agent: %d item(s) requiring attention", total)

        if total == 0:
            # If a digest exists but there's nothing to escalate, close it
            await self._close_resolved_digest()
            return report

        body = self._build_digest_body(buckets)

        try:
            issue_number = await self._upsert_digest(body)
            report.digest_issue_number = issue_number
        except Exception as e:
            logger.error("Escalation agent: digest upsert failed: %s", e)
            report.errors.append(str(e))

        return report

    async def _upsert_digest(self, body: str) -> int:
        await self._issues.ensure_label(
            ESCALATION_DIGEST_LABEL,
            color="b91c1c",
            description="Weekly human-action-required digest",
        )

        existing = await self._issues.list(state="open", labels=ESCALATION_DIGEST_LABEL)
        digest_issues = [i for i in existing if ESCALATION_AGENT_MARKER in (i.body or "")]

        if digest_issues:
            issue = digest_issues[0]
            await self._issues.update(issue.number, body=body)
            logger.info("Escalation agent: updated digest issue #%d", issue.number)
            return issue.number

        assignees = self._notify_assignees or []
        issue = await self._issues.create(
            title=f"[Caretaker] Human action required — {datetime.now(UTC).strftime('%Y-W%V')}",
            body=body,
            labels=[ESCALATION_DIGEST_LABEL],
            assignees=assignees if assignees else None,
        )
        number = issue["number"] if isinstance(issue, dict) else issue.number
        logger.info("Escalation agent: created digest issue #%d", number)
        return number

    async def _close_resolved_digest(self) -> None:
        """Close any existing digest if all items are resolved."""
        try:
            existing = await self._issues.list(state="open", labels=ESCALATION_DIGEST_LABEL)
            for issue in existing:
                if ESCALATION_AGENT_MARKER in (issue.body or ""):
                    await self._issues.update(
                        issue.number,
                        state="closed",
                        state_reason="completed",
                    )
                    logger.info("Escalation agent: closed resolved digest #%d", issue.number)
        except Exception as e:
            logger.warning("Escalation agent: could not close digest: %s", e)

    def _build_digest_body(self, buckets: dict[str, list[Any]]) -> str:
        week = datetime.now(UTC).strftime("%Y-W%V")
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        lines = [
            f"## 🔔 Caretaker — Human Action Required — {week} ({date})\n",
            "The following items have been identified as requiring manual maintainer review.\n",
        ]

        for label, items in sorted(buckets.items()):
            description = _ACTION_LABELS.get(label, label)
            lines.append(f"\n### {description} (`{label}`)\n")
            lines.append("| # | Title | Age |")
            lines.append("|---|---|---|")
            for item in sorted(items, key=lambda i: i.number):
                updated_at = getattr(item, "updated_at", None) or (
                    getattr(item, "raw", {}).get("updated_at", "") if hasattr(item, "raw") else ""
                )
                age = _age_str(updated_at)
                lines.append(f"| #{item.number} | {item.title} | {age} |")

        if self._notify_assignees:
            mention_str = " ".join(f"@{a}" for a in self._notify_assignees)
            lines.append(f"\n---\n📣 {mention_str} — please review the items above.")

        lines.append(f"\n---\n{ESCALATION_AGENT_MARKER} week:{week} -->")
        return "\n".join(lines)


def _age_str(updated_at: str | None) -> str:
    if not updated_at:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        days = (datetime.now(UTC) - dt).days
        if days == 0:
            return "today"
        if days == 1:
            return "1 day"
        return f"{days} days"
    except (ValueError, TypeError):
        return "unknown"
