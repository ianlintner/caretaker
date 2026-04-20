"""Tests for the escalation agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.escalation_agent.agent import (
    ESCALATION_AGENT_MARKER,
    ESCALATION_DIGEST_LABEL,
    EscalationAgent,
)
from caretaker.github_client.models import Issue, Label, User


def _issue(
    number: int,
    title: str = "Some issue",
    body: str = "",
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        user=User(login="dev", id=1),
        labels=[Label(name=n) for n in (labels or [])],
        assignees=[],
        html_url=f"https://github.com/o/r/issues/{number}",
    )


def make_github(
    items_by_label: dict[str, list] | None = None,
    digest_issues: list | None = None,
) -> AsyncMock:
    """
    items_by_label: label -> list of Issue objects returned for that label.
    digest_issues: open digest issues (returned when listing ESCALATION_DIGEST_LABEL).
    """
    items_by_label = items_by_label or {}
    digest_issues = digest_issues or []

    async def _list_issues(owner, repo, state="open", labels=None):
        if labels == ESCALATION_DIGEST_LABEL:
            return digest_issues
        if labels and labels in items_by_label:
            return items_by_label[labels]
        return []

    gh = AsyncMock()
    gh.list_issues.side_effect = _list_issues
    gh.ensure_label.return_value = None
    gh.create_issue.return_value = {"number": 42}
    gh.update_issue.return_value = None
    return gh


# ── EscalationAgent tests ────────────────────────────────────────────


class TestEscalationAgentCreatesDigest:
    @pytest.mark.asyncio
    async def test_creates_digest_when_items_found(self) -> None:
        gh = make_github(
            items_by_label={"security:finding": [_issue(10, "Critical CVE")]},
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.items_found == 1
        assert report.digest_issue_number == 42
        gh.create_issue.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_digest_body_contains_label_bucket(self) -> None:
        gh = make_github(
            items_by_label={"security:finding": [_issue(10, "Critical CVE")]},
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        await agent.run()

        body = gh.create_issue.call_args.kwargs["body"]
        assert "security:finding" in body or "Security finding" in body

    @pytest.mark.asyncio
    async def test_digest_body_contains_debug_dump(self) -> None:
        gh = make_github(
            items_by_label={"security:finding": [_issue(10, "Critical CVE")]},
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        await agent.run()

        body = gh.create_issue.call_args.kwargs["body"]
        assert "Digest debug dump" in body
        assert '"bucket_counts"' in body
        assert '"item_numbers_by_label"' in body

    @pytest.mark.asyncio
    async def test_digest_body_contains_causal_marker(self) -> None:
        gh = make_github(
            items_by_label={"security:finding": [_issue(10, "Critical CVE")]},
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        await agent.run()

        body = gh.create_issue.call_args.kwargs["body"]
        assert "caretaker:causal" in body
        assert "source=escalation-agent:digest" in body

    @pytest.mark.asyncio
    async def test_assigns_notify_assignees(self) -> None:
        gh = make_github(
            items_by_label={"help wanted": [_issue(5, "Need help")]},
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r", notify_assignees=["maintainer1"])
        await agent.run()

        call_kwargs = gh.create_issue.call_args.kwargs
        assert "maintainer1" in call_kwargs.get("assignees", [])


class TestEscalationAgentUpdateDigest:
    @pytest.mark.asyncio
    async def test_updates_existing_digest_instead_of_creating(self) -> None:
        existing = _issue(
            77,
            title="[Caretaker] Human action required",
            body=f"{ESCALATION_AGENT_MARKER}\n-->",
            labels=[ESCALATION_DIGEST_LABEL],
        )
        gh = make_github(
            items_by_label={"devops:build-failure": [_issue(11, "CI broken")]},
            digest_issues=[existing],
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        # Should update, not create
        gh.update_issue.assert_awaited_once()
        gh.create_issue.assert_not_awaited()
        assert report.digest_issue_number == 77

    @pytest.mark.asyncio
    async def test_updated_body_contains_marker(self) -> None:
        existing = _issue(
            77,
            body=f"{ESCALATION_AGENT_MARKER}\n-->",
            labels=[ESCALATION_DIGEST_LABEL],
        )
        gh = make_github(
            items_by_label={"devops:build-failure": [_issue(11, "CI broken")]},
            digest_issues=[existing],
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        await agent.run()

        body = gh.update_issue.call_args.kwargs["body"]
        assert ESCALATION_AGENT_MARKER in body


class TestEscalationAgentNoItems:
    @pytest.mark.asyncio
    async def test_no_digest_when_no_items(self) -> None:
        gh = make_github()
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.items_found == 0
        assert report.digest_issue_number is None
        gh.create_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_closes_resolved_digest_when_no_items(self) -> None:
        existing = _issue(
            77,
            body=f"{ESCALATION_AGENT_MARKER}\n-->",
            labels=[ESCALATION_DIGEST_LABEL],
        )
        gh = make_github(digest_issues=[existing])
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        await agent.run()

        # Should close the open digest
        gh.update_issue.assert_awaited()
        close_kwargs = gh.update_issue.call_args.kwargs
        assert close_kwargs.get("state") == "closed"


class TestEscalationAgentMultipleBuckets:
    @pytest.mark.asyncio
    async def test_counts_across_multiple_labels(self) -> None:
        gh = make_github(
            items_by_label={
                "security:finding": [_issue(1, "CVE-1"), _issue(2, "CVE-2")],
                "dependencies:major-upgrade": [_issue(3, "Major bump")],
            }
        )
        agent = EscalationAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.items_found == 3
