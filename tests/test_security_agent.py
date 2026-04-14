"""Tests for the security agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.github_client.models import Issue, User
from caretaker.security_agent.agent import (
    SECURITY_AGENT_MARKER,
    AlertKind,
    SecurityAgent,
    SecurityFinding,
    Severity,
    _finding_signature,
)


def make_github(
    dependabot_alerts: list[dict] | None = None,
    code_scanning_alerts: list[dict] | None = None,
    secret_scanning_alerts: list[dict] | None = None,
    existing_issues: list | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_dependabot_alerts.return_value = dependabot_alerts or []
    gh.list_code_scanning_alerts.return_value = code_scanning_alerts or []
    gh.list_secret_scanning_alerts.return_value = secret_scanning_alerts or []
    gh.ensure_label.return_value = None
    gh.list_issues.return_value = existing_issues or []
    gh.create_issue.return_value = Issue(
        number=42,
        title="Security",
        body="",
        state="open",
        user=User(login="bot", id=1),
        labels=[],
        assignees=[],
        html_url="https://github.com/o/r/issues/42",
    )
    return gh


# ── Severity tests ───────────────────────────────────────────────────


class TestSeverity:
    def test_from_str_normalises_case(self) -> None:
        assert Severity.from_str("CRITICAL") == Severity.CRITICAL
        assert Severity.from_str("high") == Severity.HIGH
        assert Severity.from_str("Medium") == Severity.MEDIUM

    def test_from_str_unknown(self) -> None:
        assert Severity.from_str("banana") == Severity.UNKNOWN

    def test_ordering(self) -> None:
        assert Severity.CRITICAL < Severity.HIGH
        assert Severity.HIGH < Severity.MEDIUM
        assert Severity.MEDIUM < Severity.LOW
        assert Severity.LOW < Severity.UNKNOWN


# ── SecurityAgent tests ──────────────────────────────────────────────


_DEPENDABOT_ALERT = {
    "number": 1,
    "state": "open",
    "dependency": {
        "package": {"name": "requests", "ecosystem": "pip"},
    },
    "security_advisory": {
        "summary": "SSRF in requests",
        "description": "Detailed description",
        "severity": "high",
    },
    "html_url": "https://github.com/o/r/security/dependabot/1",
    "dismissed_reason": None,
}

_CODE_SCANNING_ALERT = {
    "number": 5,
    "state": "open",
    "rule": {"id": "py/sql-injection", "name": "SQL injection", "severity": "critical"},
    "most_recent_instance": {"message": {"text": "Unsanitised input flow"}},
    "html_url": "https://github.com/o/r/security/code-scanning/5",
    "dismissed_reason": None,
}


class TestSecurityAgentCollectsAlerts:
    @pytest.mark.asyncio
    async def test_creates_issue_for_high_dependabot_alert(self) -> None:
        gh = make_github(dependabot_alerts=[_DEPENDABOT_ALERT])
        agent = SecurityAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.findings_found == 1
        assert len(report.issues_created) == 1
        gh.create_issue.assert_awaited_once()
        call_kwargs = gh.create_issue.call_args.kwargs
        assert "copilot" in call_kwargs.get("assignees", [])

    @pytest.mark.asyncio
    async def test_skips_below_min_severity(self) -> None:
        alert = {
            **_DEPENDABOT_ALERT,
            "security_advisory": {**_DEPENDABOT_ALERT["security_advisory"], "severity": "low"},
        }
        gh = make_github(dependabot_alerts=[alert])
        agent = SecurityAgent(github=gh, owner="o", repo="r", min_severity="high")
        report = await agent.run()

        assert len(report.issues_created) == 0

    @pytest.mark.asyncio
    async def test_deduplicates_existing_issue(self) -> None:
        sig = _finding_signature(
            SecurityFinding(
                kind=AlertKind.DEPENDABOT,
                alert_number=1,
                title="SSRF in requests",
                severity=Severity.HIGH,
                package="requests",
                description="",
                html_url="",
                raw={},
            )
        )
        existing = [
            Issue(
                number=10,
                title="[Security] SSRF in requests",
                body=f"{SECURITY_AGENT_MARKER} sig:{sig} -->",
                state="open",
                user=User(login="bot", id=1),
                labels=[],
                assignees=[],
                html_url="",
            )
        ]
        gh = make_github(dependabot_alerts=[_DEPENDABOT_ALERT], existing_issues=existing)
        agent = SecurityAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.issues_skipped == 1
        gh.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_code_scanning_alert_creates_issue(self) -> None:
        gh = make_github(code_scanning_alerts=[_CODE_SCANNING_ALERT])
        agent = SecurityAgent(github=gh, owner="o", repo="r", include_dependabot=False)
        report = await agent.run()

        assert report.findings_found == 1
        assert len(report.issues_created) == 1

    @pytest.mark.asyncio
    async def test_include_flags_disable_sources(self) -> None:
        gh = make_github(
            dependabot_alerts=[_DEPENDABOT_ALERT],
            code_scanning_alerts=[_CODE_SCANNING_ALERT],
        )
        agent = SecurityAgent(
            github=gh,
            owner="o",
            repo="r",
            include_dependabot=False,
            include_code_scanning=False,
            include_secret_scanning=False,
        )
        report = await agent.run()

        assert report.findings_found == 0
        gh.list_dependabot_alerts.assert_not_awaited()
