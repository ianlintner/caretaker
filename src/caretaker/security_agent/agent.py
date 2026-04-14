"""Security Agent — triages Dependabot security, CodeQL, and secret-scanning alerts."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from caretaker.tools.github import GitHubIssueTools

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue

from caretaker.github_client.api import GitHubAPIError

logger = logging.getLogger(__name__)

SECURITY_LABEL = "security:finding"
SECURITY_FP_LABEL = "security:false-positive"
SECURITY_AGENT_MARKER = "<!-- caretaker:security-agent"

# Numeric priority for Severity comparison: smaller = more severe
_SEVERITY_PRIORITY = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "unknown": 4,
}


class AlertKind(StrEnum):
    DEPENDABOT = "dependabot"
    CODE_SCANNING = "code_scanning"
    SECRET_SCANNING = "secret_scanning"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

    @classmethod
    def from_str(cls, value: str | None) -> Severity:
        if not value:
            return cls.UNKNOWN
        v = value.lower()
        if v in ("critical",):
            return cls.CRITICAL
        if v in ("high",):
            return cls.HIGH
        if v in ("medium", "moderate"):
            return cls.MEDIUM
        if v in ("low",):
            return cls.LOW
        return cls.UNKNOWN

    def __lt__(self, other: Severity) -> bool:  # type: ignore[override]
        return _SEVERITY_PRIORITY.get(self.value, 4) < _SEVERITY_PRIORITY.get(other.value, 4)

    def __le__(self, other: Severity) -> bool:  # type: ignore[override]
        return self == other or self.__lt__(other)

    def __gt__(self, other: Severity) -> bool:  # type: ignore[override]
        return not self.__le__(other)

    def __ge__(self, other: Severity) -> bool:  # type: ignore[override]
        return not self.__lt__(other)


@dataclass
class SecurityFinding:
    """Represents a single security alert from any source."""

    kind: AlertKind
    alert_number: int
    title: str
    severity: Severity
    package: str  # package / rule / secret type
    description: str
    html_url: str
    raw: dict[str, Any]  # original API payload


@dataclass
class SecurityReport:
    """Results from a single Security agent run."""

    findings_found: int = 0
    issues_created: list[int] = field(default_factory=list)
    issues_skipped: int = 0  # duplicate / below threshold
    false_positives_flagged: int = 0
    errors: list[str] = field(default_factory=list)


class SecurityAgent:
    """
    Triages GitHub security alerts:
    * Dependabot security advisories
    * CodeQL / GHAS code-scanning alerts
    * Secret-scanning alerts

    For each finding above *min_severity*:
    - Creates a tracked issue assigned to @copilot with a remediation plan.
    - Deduplicates: skips if an open issue with the same alert identifier exists.
    - Optionally auto-dismisses alerts whose package/rule matches *false_positive_rules*.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        min_severity: str = "medium",
        max_issues_per_run: int = 5,
        false_positive_rules: list[str] | None = None,
        include_dependabot: bool = True,
        include_code_scanning: bool = True,
        include_secret_scanning: bool = True,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._min_severity = Severity.from_str(min_severity)
        self._max_issues_per_run = max_issues_per_run
        self._fp_rules: list[str] = [r.lower() for r in (false_positive_rules or [])]
        self._include_dependabot = include_dependabot
        self._include_code_scanning = include_code_scanning
        self._include_secret_scanning = include_secret_scanning
        self._issues = GitHubIssueTools(github, owner, repo)

    _SEVERITY_ORDER = {
        Severity.CRITICAL: 4,
        Severity.HIGH: 3,
        Severity.MEDIUM: 2,
        Severity.LOW: 1,
        Severity.UNKNOWN: 0,
    }

    @staticmethod
    def _finding_signature(finding: SecurityFinding) -> str:
        """Return the deduplication signature for a finding."""
        return _finding_signature(finding)

    async def run(self) -> SecurityReport:
        """Collect all open security alerts and create/manage tracking issues."""
        report = SecurityReport()

        findings: list[SecurityFinding] = []
        if self._include_dependabot:
            try:
                findings.extend(await self._collect_dependabot_alerts())
            except Exception as e:
                if _is_feature_unavailable(e):
                    logger.info("Security agent: dependabot alerts unavailable: %s", e)
                else:
                    logger.warning("Security agent: dependabot alerts error: %s", e)
                    report.errors.append(f"dependabot: {e}")

        if self._include_code_scanning:
            try:
                findings.extend(await self._collect_code_scanning_alerts())
            except Exception as e:
                if _is_feature_unavailable(e):
                    logger.info("Security agent: code scanning unavailable: %s", e)
                else:
                    logger.warning("Security agent: code scanning error: %s", e)
                    report.errors.append(f"code_scanning: {e}")

        if self._include_secret_scanning:
            try:
                findings.extend(await self._collect_secret_scanning_alerts())
            except Exception as e:
                if _is_feature_unavailable(e):
                    logger.info("Security agent: secret scanning unavailable: %s", e)
                else:
                    logger.warning("Security agent: secret scanning error: %s", e)
                    report.errors.append(f"secret_scanning: {e}")

        # Filter to actionable findings
        findings = [f for f in findings if self._is_actionable(f)]
        report.findings_found = len(findings)

        if not findings:
            logger.info("Security agent: no actionable findings")
            return report

        # Sort by descending severity
        findings.sort(key=lambda f: self._SEVERITY_ORDER.get(f.severity, 0), reverse=True)

        existing_sigs = await self._get_existing_issue_signatures()

        created = 0
        for finding in findings:
            if created >= self._max_issues_per_run:
                break

            if self._is_false_positive(finding):
                try:
                    await self._dismiss_as_false_positive(finding)
                    report.false_positives_flagged += 1
                except Exception as e:
                    logger.warning(
                        "Security agent: FP dismiss failed for %s: %s",
                        finding.alert_number,
                        e,
                    )
                continue

            sig = _finding_signature(finding)
            if sig in existing_sigs:
                report.issues_skipped += 1
                continue

            try:
                issue = await self._create_security_issue(finding, sig)
                issue_num = issue["number"] if isinstance(issue, dict) else issue.number
                report.issues_created.append(issue_num)
                created += 1
            except Exception as e:
                logger.error(
                    "Security agent: failed to create issue for %s: %s",
                    finding.title,
                    e,
                )
                report.errors.append(str(e))

        return report

    def _is_actionable(self, finding: SecurityFinding) -> bool:
        return self._SEVERITY_ORDER.get(finding.severity, 0) >= self._SEVERITY_ORDER.get(
            self._min_severity, 0
        )

    def _is_false_positive(self, finding: SecurityFinding) -> bool:
        pkg_lower = finding.package.lower()
        return any(rule in pkg_lower for rule in self._fp_rules)

    async def _collect_dependabot_alerts(self) -> list[SecurityFinding]:
        alerts = await self._github.list_dependabot_alerts(self._owner, self._repo, state="open")
        findings = []
        for a in alerts:
            adv = a.get("security_advisory", {})
            package = a.get("dependency", {}).get("package", {}).get("name", "unknown")
            severity = (
                adv.get("severity")
                or a.get("security_vulnerability", {}).get("severity")
                or a.get("severity")
                or "unknown"
            )
            findings.append(
                SecurityFinding(
                    kind=AlertKind.DEPENDABOT,
                    alert_number=a["number"],
                    title=f"[Dependabot] {adv.get('summary', package)}",
                    severity=Severity.from_str(severity),
                    package=package,
                    description=adv.get("description", ""),
                    html_url=a.get("html_url", ""),
                    raw=a,
                )
            )
        return findings

    async def _collect_code_scanning_alerts(self) -> list[SecurityFinding]:
        alerts = await self._github.list_code_scanning_alerts(self._owner, self._repo, state="open")
        findings = []
        for a in alerts:
            rule = a.get("rule", {})
            severity = (
                rule.get("severity")
                or a.get("most_recent_instance", {}).get("severity")
                or "unknown"
            )
            findings.append(
                SecurityFinding(
                    kind=AlertKind.CODE_SCANNING,
                    alert_number=a["number"],
                    title=f"[CodeQL] {rule.get('description', rule.get('id', 'unknown'))}",
                    severity=Severity.from_str(severity),
                    package=rule.get("id", "unknown"),
                    description=rule.get("full_description", ""),
                    html_url=a.get("html_url", ""),
                    raw=a,
                )
            )
        return findings

    async def _collect_secret_scanning_alerts(self) -> list[SecurityFinding]:
        alerts = await self._github.list_secret_scanning_alerts(
            self._owner, self._repo, state="open"
        )
        findings = []
        for a in alerts:
            secret_label = a.get("secret_type_display_name", a.get("secret_type", "secret"))
            findings.append(
                SecurityFinding(
                    kind=AlertKind.SECRET_SCANNING,
                    alert_number=a["number"],
                    title=f"[Secret] {secret_label} exposed",
                    severity=Severity.CRITICAL,  # all live secrets are critical
                    package=a.get("secret_type", "unknown"),
                    description=(
                        f"A `{a.get('secret_type_display_name', 'secret')}` was detected "
                        f"in the repository. Immediate rotation required."
                    ),
                    html_url=a.get("html_url", ""),
                    raw=a,
                )
            )
        return findings

    async def _get_existing_issue_signatures(self) -> set[str]:
        existing = await self._issues.list(state="open", labels=SECURITY_LABEL)
        sigs: set[str] = set()
        for issue in existing:
            body = issue.body or ""
            for line in body.splitlines():
                if line.startswith(SECURITY_AGENT_MARKER) and "sig:" in line:
                    # extract sig:<value>
                    raw = line.split("sig:")[1].strip()
                    sigs.add(raw.split()[0])
        return sigs

    async def _dismiss_as_false_positive(self, finding: SecurityFinding) -> None:
        comment = "Automatically flagged as false positive by caretaker security agent."
        if finding.kind == AlertKind.DEPENDABOT:
            await self._github.dismiss_dependabot_alert(
                self._owner,
                self._repo,
                finding.alert_number,
                reason="tolerable_risk",
                comment=comment,
            )
        elif finding.kind == AlertKind.CODE_SCANNING:
            await self._github.dismiss_code_scanning_alert(
                self._owner,
                self._repo,
                finding.alert_number,
                reason="false positive",
                comment=comment,
            )
        # Secret scanning alerts cannot be dismissed automatically without human confirmation

    async def _create_security_issue(self, finding: SecurityFinding, sig: str) -> Issue:
        sev_emoji = {
            Severity.CRITICAL: "🔴",
            Severity.HIGH: "🟠",
            Severity.MEDIUM: "🟡",
            Severity.LOW: "🔵",
            Severity.UNKNOWN: "⚪",
        }.get(finding.severity, "⚪")

        kind_label = {
            AlertKind.DEPENDABOT: "Dependabot security advisory",
            AlertKind.CODE_SCANNING: "CodeQL code-scanning finding",
            AlertKind.SECRET_SCANNING: "Secret-scanning alert",
        }.get(finding.kind, "Security finding")

        body = f"""{sev_emoji} **Severity:** {finding.severity.value.upper()}
**Type:** {kind_label}
**Package / Rule:** `{finding.package}`
**Alert:** [{finding.alert_number}]({finding.html_url})

## Summary

{finding.description or "_No description provided._"}

## Remediation

<!-- caretaker:security-assignment -->
**Resolution steps for @copilot:**

1. Review the alert at {finding.html_url}
2. If this is a **dependency vulnerability**: bump `{finding.package}` to a patched
   version; update lockfile.
3. If this is a **code-scanning finding**: fix the flagged code pattern.
4. If this is a **secret exposure**: rotate the credential immediately and
   remove it from git history.
5. Open a pull request with the fix and reference this issue.
6. Ensure CI passes before requesting review.
<!-- /caretaker:security-assignment -->

---
{SECURITY_AGENT_MARKER} sig:{sig} -->"""

        await self._issues.ensure_label(
            SECURITY_LABEL,
            color="e11d48",
            description="Security finding requiring remediation",
        )

        return await self._issues.create(
            title=f"[Security] {finding.title}",
            body=body,
            labels=[SECURITY_LABEL],
            assignees=["copilot"],
            copilot_assignment=self._issues.default_copilot_assignment(),
        )


def _finding_signature(finding: SecurityFinding) -> str:
    raw = f"{finding.kind.value}:{finding.alert_number}:{finding.package}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _is_feature_unavailable(exc: Exception) -> bool:
    """Return True when the error indicates a GitHub feature is disabled or inaccessible."""
    return isinstance(exc, GitHubAPIError) and exc.status_code in (403, 404)
