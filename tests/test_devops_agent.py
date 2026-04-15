"""Tests for the DevOps agent duplicate suppression."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.devops_agent.agent import (
    BUILD_FAILURE_LABEL,
    DEVOPS_AGENT_MARKER,
    DevOpsAgent,
    _build_issue_body,
    _failure_signature,
)
from caretaker.devops_agent.log_analyzer import FailureSummary
from caretaker.github_client.models import Issue, Label, User


def _issue_with_signature(number: int, sig: str) -> Issue:
    return Issue(
        number=number,
        title="CI failure",
        body=f"{DEVOPS_AGENT_MARKER} sig:{sig} -->",
        state="open",
        user=User(login="app/github-actions", id=1, type="Bot"),
        labels=[Label(name=BUILD_FAILURE_LABEL)],
        html_url=f"https://github.com/o/r/issues/{number}",
    )


class TestDevOpsAgentDuplicateSuppression:
    @pytest.mark.asyncio
    async def test_extracts_raw_signature_from_existing_issue_marker(self) -> None:
        gh = AsyncMock()
        gh.list_issues.return_value = [_issue_with_signature(1, "deadbeefcafe")]
        agent = DevOpsAgent(github=gh, owner="o", repo="r")

        signatures = await agent._get_existing_failure_signatures()

        assert signatures == {"deadbeefcafe"}

    @pytest.mark.asyncio
    async def test_run_skips_creating_duplicate_issue_when_signature_matches(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/caretaker/config.py"],
            category="lint",
        )
        signature = _failure_signature(summary)

        gh = AsyncMock()
        gh.list_issues.return_value = [_issue_with_signature(2, signature)]
        agent = DevOpsAgent(github=gh, owner="o", repo="r")
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock()  # type: ignore[method-assign]

        report = await agent.run()

        assert report.failures_detected == 1
        assert report.issues_skipped == 1
        assert report.issues_created == []
        agent._create_fix_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_known_sigs_prevents_duplicate_creation(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/caretaker/config.py"],
            category="lint",
        )
        signature = _failure_signature(summary)

        gh = AsyncMock()
        gh.list_issues.return_value = []  # No open issues
        agent = DevOpsAgent(github=gh, owner="o", repo="r", known_sigs={signature})
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock()  # type: ignore[method-assign]

        report = await agent.run()

        assert report.failures_detected == 1
        assert report.issues_skipped == 1
        assert report.issues_created == []
        agent._create_fix_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_actioned_sigs_tracked_on_issue_creation(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/caretaker/config.py"],
            category="lint",
        )
        created_issue = Issue(
            number=10,
            title="CI failure",
            body="",
            state="open",
            user=User(login="bot", id=1, type="Bot"),
            html_url="https://github.com/o/r/issues/10",
        )

        gh = AsyncMock()
        gh.list_issues.return_value = []
        agent = DevOpsAgent(github=gh, owner="o", repo="r")
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock(return_value=created_issue)  # type: ignore[method-assign]

        report = await agent.run()

        assert len(report.actioned_sigs) == 1
        assert report.actioned_sigs[0] == _failure_signature(summary)

    @pytest.mark.asyncio
    async def test_cross_agent_run_id_dedup_skips_when_run_already_tracked(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/foo.py"],
            category="lint",
        )
        # A self-heal issue already exists with this run_id
        self_heal_issue = Issue(
            number=5,
            title="Self-heal issue",
            body="<!-- caretaker:self-heal --> sig:abc123 run_id:12345 -->",
            state="open",
            user=User(login="bot", id=1, type="Bot"),
            labels=[Label(name="caretaker:self-heal")],
            html_url="https://github.com/o/r/issues/5",
        )

        gh = AsyncMock()
        # First call: devops issues (empty), second call: self-heal issues
        gh.list_issues.side_effect = [[], [self_heal_issue]]
        agent = DevOpsAgent(github=gh, owner="o", repo="r")
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock()  # type: ignore[method-assign]

        payload = {"workflow_run": {"id": 12345, "conclusion": "failure", "head_branch": "main"}}
        report = await agent.run(event_payload=payload)

        assert report.failures_detected == 1
        assert report.issues_skipped == 1
        assert report.issues_created == []
        agent._create_fix_issue.assert_not_awaited()


class TestDevOpsIssueBodyRunId:
    def test_build_issue_body_includes_run_id_when_provided(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/foo.py"],
            category="lint",
        )
        sig = _failure_signature(summary)
        body = _build_issue_body(summary, sig, "main", run_id=99001)
        assert "run_id:99001" in body
        assert f"sig:{sig}" in body

    def test_build_issue_body_omits_run_id_when_none(self) -> None:
        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/foo.py"],
            category="lint",
        )
        sig = _failure_signature(summary)
        body = _build_issue_body(summary, sig, "main")
        assert "run_id:" not in body
        assert f"sig:{sig}" in body

    @pytest.mark.asyncio
    async def test_run_passes_run_id_from_event_payload(self) -> None:
        summary = FailureSummary(
            job_name="test",
            conclusion="failure",
            suspected_files=[],
            category="test",
        )
        gh = AsyncMock()
        gh.list_issues.return_value = []
        agent = DevOpsAgent(github=gh, owner="o", repo="r")
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]

        created_issue = Issue(
            number=99,
            title="test",
            body="",
            state="open",
            user=User(login="bot", id=1, type="Bot"),
            html_url="https://github.com/o/r/issues/99",
        )
        agent._create_fix_issue = AsyncMock(return_value=created_issue)  # type: ignore[method-assign]

        payload = {"workflow_run": {"id": 12345, "conclusion": "failure", "head_branch": "main"}}
        await agent.run(event_payload=payload)

        agent._create_fix_issue.assert_awaited_once()
        call_kwargs = agent._create_fix_issue.call_args
        assert call_kwargs.kwargs.get("run_id") == 12345


class TestDevOpsCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_skips_same_job_category_within_window(self) -> None:
        from datetime import UTC, datetime

        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/foo.py"],
            category="lint",
        )
        # Recent cooldown entry for the same job+category
        recent_ts = datetime.now(UTC).isoformat()
        cooldowns = {"devops:lint:lint": recent_ts}

        gh = AsyncMock()
        gh.list_issues.return_value = []
        agent = DevOpsAgent(
            github=gh, owner="o", repo="r", cooldown_hours=6, issue_cooldowns=cooldowns
        )
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock()  # type: ignore[method-assign]

        report = await agent.run()

        assert report.issues_skipped == 1
        assert report.issues_created == []
        agent._create_fix_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_window_expires(self) -> None:
        from datetime import UTC, datetime, timedelta

        summary = FailureSummary(
            job_name="lint",
            conclusion="failure",
            suspected_files=["src/foo.py"],
            category="lint",
        )
        # Old cooldown entry — well past the window
        old_ts = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        cooldowns = {"devops:lint:lint": old_ts}

        created_issue = Issue(
            number=10,
            title="CI failure",
            body="",
            state="open",
            user=User(login="bot", id=1, type="Bot"),
            html_url="https://github.com/o/r/issues/10",
        )

        gh = AsyncMock()
        gh.list_issues.return_value = []
        agent = DevOpsAgent(
            github=gh, owner="o", repo="r", cooldown_hours=6, issue_cooldowns=cooldowns
        )
        agent._discover_failing_jobs = AsyncMock(return_value=[summary])  # type: ignore[method-assign]
        agent._create_fix_issue = AsyncMock(return_value=created_issue)  # type: ignore[method-assign]

        report = await agent.run()

        assert report.issues_created == [10]
        agent._create_fix_issue.assert_awaited_once()
        # Cooldown should have been updated
        assert "devops:lint:lint" in report.updated_cooldowns
