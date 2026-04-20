"""Tests for the Charlie janitorial agent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from caretaker.charlie_agent.agent import (
    CharlieAgent,
    _build_issue_label_map,
    _build_issue_run_map,
    _extract_work_key,
    _primary_source_file,
    _resolve_pr_run_key,
)
from caretaker.github_client.models import Issue, Label, PRState, PullRequest, User


def _issue(
    number: int,
    *,
    title: str = "Regular issue",
    body: str = "",
    updated_days_ago: int = 2,
    labels: list[str] | None = None,
    assignees: list[User] | None = None,
    user: User | None = None,
) -> Issue:
    now = datetime.now(UTC)
    return Issue(
        number=number,
        title=title,
        body=body,
        state="open",
        user=user or User(login="app/github-actions", id=1, type="Bot"),
        labels=[Label(name=name) for name in (labels or [])],
        assignees=assignees or [],
        created_at=now - timedelta(days=updated_days_ago + 1),
        updated_at=now - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/issues/{number}",
    )


def _pr(
    number: int,
    *,
    title: str = "Caretaker PR",
    body: str = "",
    updated_days_ago: int = 2,
    labels: list[str] | None = None,
    draft: bool = False,
    user: User | None = None,
) -> PullRequest:
    now = datetime.now(UTC)
    return PullRequest(
        number=number,
        title=title,
        body=body,
        state=PRState.OPEN,
        user=user or User(login="Copilot", id=2, type="Bot"),
        head_ref=f"feature/{number}",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=draft,
        labels=[Label(name=name) for name in (labels or [])],
        created_at=now - timedelta(days=updated_days_ago + 1),
        updated_at=now - timedelta(days=updated_days_ago),
        html_url=f"https://github.com/o/r/pull/{number}",
    )


def make_github(
    *,
    issues: list[Issue] | None = None,
    prs: list[PullRequest] | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_issues.return_value = issues or []
    gh.list_pull_requests.return_value = prs or []
    gh.add_issue_comment.return_value = None
    gh.update_issue.return_value = None
    return gh


class TestCharlieAgent:
    @pytest.mark.asyncio
    async def test_closes_duplicate_managed_issues_using_source_issue_key(self) -> None:
        canonical = _issue(
            10,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #42",
            assignees=[User(login="Copilot", id=3, type="Bot")],
        )
        duplicate = _issue(
            11,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #42",
        )
        gh = make_github(issues=[canonical, duplicate])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_issues_seen == 2
        assert report.duplicate_issues_closed == 1
        assert report.issues_closed == 1
        gh.add_issue_comment.assert_awaited_once()
        gh.update_issue.assert_awaited_once_with("o", "r", 11, state="closed")

    @pytest.mark.asyncio
    async def test_closes_duplicate_managed_prs_using_fixes_key(self) -> None:
        canonical = _pr(20, body="Fixes #77")
        duplicate = _pr(21, body="Fixes #77", draft=True)
        gh = make_github(prs=[canonical, duplicate])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_prs_seen == 2
        assert report.duplicate_prs_closed == 1
        assert report.prs_closed == 1
        gh.update_issue.assert_awaited_once_with("o", "r", 21, state="closed")

    @pytest.mark.asyncio
    async def test_closes_stale_managed_issue_after_short_window(self) -> None:
        stale_issue = _issue(
            30,
            title="[Maintainer] Follow-up cleanup",
            updated_days_ago=21,
        )
        gh = make_github(issues=[stale_issue])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 1
        assert report.issues_closed == 1
        gh.update_issue.assert_awaited_once_with("o", "r", 30, state="closed")

    @pytest.mark.asyncio
    async def test_respects_exempt_labels_for_stale_cleanup(self) -> None:
        exempt_issue = _issue(
            31,
            title="[Maintainer] Escalated item",
            updated_days_ago=30,
            labels=["maintainer:escalated"],
        )
        gh = make_github(issues=[exempt_issue])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 0
        assert report.issues_closed == 0
        gh.add_issue_comment.assert_not_awaited()
        gh.update_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_stale_closure_for_copilot_assigned_issue(self) -> None:
        active_assignment = _issue(
            32,
            body="<!-- caretaker:assignment -->\nSOURCE_ISSUE: #99",
            updated_days_ago=30,
            assignees=[User(login="Copilot", id=4, type="Bot")],
        )
        gh = make_github(issues=[active_assignment])

        report = await CharlieAgent(github=gh, owner="o", repo="r", stale_days=14).run()

        assert report.stale_issues_closed == 0
        assert report.issues_closed == 0
        gh.update_issue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_groups_issues_by_run_id(self) -> None:
        """Issues from the same workflow run should be grouped as duplicates."""
        canonical = _issue(
            40,
            body="<!-- caretaker:devops-build-failure sig:aaa111 run_id:99001 -->",
            labels=["devops:build-failure"],
            assignees=[User(login="Copilot", id=3, type="Bot")],
        )
        duplicate = _issue(
            41,
            body="<!-- caretaker:devops-build-failure sig:bbb222 run_id:99001 -->",
            labels=["devops:build-failure"],
        )
        gh = make_github(issues=[canonical, duplicate])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_issues_seen == 2
        assert report.duplicate_issues_closed == 1
        assert report.issues_closed == 1
        gh.update_issue.assert_awaited_once_with("o", "r", 41, state="closed")

    @pytest.mark.asyncio
    async def test_groups_prs_by_linked_issue_run_id(self) -> None:
        """PRs fixing different issues from the same workflow run should be grouped."""
        issue_a = _issue(
            50,
            body="<!-- caretaker:devops-build-failure sig:aaa111 run_id:88001 -->",
            labels=["devops:build-failure"],
        )
        issue_b = _issue(
            51,
            body="<!-- caretaker:devops-build-failure sig:bbb222 run_id:88001 -->",
            labels=["devops:build-failure"],
        )
        pr_a = _pr(60, body="Fixes #50")
        pr_b = _pr(61, body="Fixes #51", draft=True)
        gh = make_github(issues=[issue_a, issue_b], prs=[pr_a, pr_b])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_prs_seen == 2
        assert report.duplicate_prs_closed == 1
        assert report.prs_closed == 1
        # Draft PR #61 should be closed in favor of non-draft #60
        gh.update_issue.assert_awaited()

    @pytest.mark.asyncio
    async def test_does_not_group_issues_with_different_run_ids(self) -> None:
        """Issues from different workflow runs should NOT be grouped."""
        issue_a = _issue(
            70,
            body="<!-- caretaker:devops-build-failure sig:ccc333 run_id:11111 -->",
            labels=["devops:build-failure"],
        )
        issue_b = _issue(
            71,
            body="<!-- caretaker:devops-build-failure sig:ddd444 run_id:22222 -->",
            labels=["devops:build-failure"],
        )
        gh = make_github(issues=[issue_a, issue_b])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.managed_issues_seen == 2
        assert report.duplicate_issues_closed == 0
        gh.update_issue.assert_not_awaited()


class TestExtractWorkKey:
    def test_run_id_takes_priority_over_devops_sig(self) -> None:
        body = "<!-- caretaker:devops-build-failure sig:abc123 run_id:99001 -->"
        assert _extract_work_key("CI failure", body) == "run_id:99001"

    def test_returns_devops_sig_when_no_run_id(self) -> None:
        body = "<!-- caretaker:devops-build-failure sig:abc123 -->"
        assert _extract_work_key("CI failure", body) == "devops_sig:abc123"

    def test_returns_run_id_key_when_no_higher_priority_match(self) -> None:
        body = "some body text\nrun_id:55555\nmore text"
        assert _extract_work_key("Some title", body) == "run_id:55555"

    def test_returns_none_for_body_without_markers(self) -> None:
        assert _extract_work_key("Regular title", "plain body") is None


class TestBuildIssueRunMap:
    def test_extracts_run_ids_from_issues(self) -> None:
        issues = [
            _issue(1, body="<!-- caretaker:devops-build-failure sig:a run_id:100 -->"),
            _issue(2, body="no marker here"),
            _issue(3, body="<!-- caretaker:self-heal --> sig:b run_id:200 -->"),
        ]
        result = _build_issue_run_map(issues)
        assert result == {1: "100", 3: "200"}

    def test_returns_empty_for_no_run_ids(self) -> None:
        issues = [_issue(1, body="just text")]
        assert _build_issue_run_map(issues) == {}


class TestResolvePrRunKey:
    def test_resolves_pr_to_run_id_via_linked_issue(self) -> None:
        issue_run_map = {50: "99001", 51: "99001"}
        assert _resolve_pr_run_key("Fixes #50", issue_run_map) == "run_id:99001"

    def test_returns_none_when_linked_issue_has_no_run_id(self) -> None:
        issue_run_map = {50: "99001"}
        assert _resolve_pr_run_key("Fixes #99", issue_run_map) is None

    def test_returns_none_when_no_fixes_reference(self) -> None:
        issue_run_map = {50: "99001"}
        assert _resolve_pr_run_key("Some PR body without fixes", issue_run_map) is None


class TestPrimarySourceFile:
    def test_picks_largest_non_test_non_doc_file(self) -> None:
        files = [
            {"path": "src/caretaker/state/tracker.py", "additions": 95, "deletions": 15},
            {"path": "src/caretaker/self_heal_agent/agent.py", "additions": 20, "deletions": 0},
            {"path": "tests/test_state_tracker.py", "additions": 85, "deletions": 0},
            {"path": "docs/changelog.md", "additions": 5, "deletions": 0},
        ]
        assert _primary_source_file(files) == "src/caretaker/state/tracker.py"

    def test_returns_none_when_only_tests_and_docs(self) -> None:
        files = [
            {"path": "tests/test_foo.py", "additions": 100, "deletions": 0},
            {"path": "docs/guide.md", "additions": 50, "deletions": 0},
            {"path": "uv.lock", "additions": 10, "deletions": 0},
        ]
        assert _primary_source_file(files) is None

    def test_returns_none_for_empty_list(self) -> None:
        assert _primary_source_file([]) is None

    def test_tiebreaks_by_path_alphabetically(self) -> None:
        files = [
            {"path": "src/b.py", "additions": 10, "deletions": 0},
            {"path": "src/a.py", "additions": 10, "deletions": 0},
        ]
        assert _primary_source_file(files) == "src/a.py"


class TestBuildIssueLabelMap:
    def test_maps_issues_to_their_labels(self) -> None:
        issues = [
            _issue(10, labels=["caretaker:self-heal", "bug"]),
            _issue(11, labels=["maintainer:internal"]),
            _issue(12, labels=[]),
        ]
        result = _build_issue_label_map(issues)
        assert result[10] == frozenset({"caretaker:self-heal", "bug"})
        assert result[11] == frozenset({"maintainer:internal"})
        assert result[12] == frozenset()


class TestSelfHealPrimaryFileDedupe:
    """Generalizes the D2 upgrade dedupe pattern to self-heal PR races.

    When three Copilot PRs all close different self-heal-labeled issues but
    all primarily touch the same source file, they are racing on the same
    underlying bug and the newer ones should be closed.
    """

    @pytest.mark.asyncio
    async def test_closes_duplicate_self_heal_prs_by_primary_file(self) -> None:
        self_heal_issues = [
            _issue(410, labels=["caretaker:self-heal", "bug"]),
            _issue(412, labels=["caretaker:self-heal", "bug"]),
            _issue(413, labels=["caretaker:self-heal", "bug"]),
        ]
        pr_411 = _pr(411, title="fix: handle 2500-comment limit")
        pr_415 = _pr(415, title="fix(state): auto-rotate tracking issue")
        pr_416 = _pr(416, title="fix: handle 2500-comment limit gracefully")
        gh = make_github(issues=self_heal_issues, prs=[pr_411, pr_415, pr_416])

        closing_by_pr = {411: [410], 415: [412], 416: [413]}
        files_by_pr = {
            411: [
                {"path": "src/caretaker/state/tracker.py", "additions": 95, "deletions": 15},
                {"path": "tests/test_state_tracker.py", "additions": 85, "deletions": 0},
            ],
            415: [
                {"path": "src/caretaker/state/tracker.py", "additions": 39, "deletions": 7},
                {"path": "src/caretaker/orchestrator.py", "additions": 4, "deletions": 1},
            ],
            416: [
                {"path": "src/caretaker/state/tracker.py", "additions": 49, "deletions": 14},
                {"path": "src/caretaker/github_client/api.py", "additions": 7, "deletions": 0},
            ],
        }
        gh.get_closing_issue_numbers.side_effect = lambda owner, repo, n: closing_by_pr.get(n, [])
        gh.list_pull_request_files.side_effect = lambda owner, repo, n: files_by_pr.get(n, [])

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        assert report.duplicate_prs_closed == 2
        closed_numbers = {
            call.args[2]
            for call in gh.update_issue.await_args_list
            if call.kwargs.get("state") == "closed"
        }
        # Charlie keeps the canonical (oldest, lowest-number) PR and closes
        # the rest. Here #411 is oldest → kept; #415 and #416 close.
        assert 415 in closed_numbers
        assert 416 in closed_numbers
        assert 411 not in closed_numbers

    @pytest.mark.asyncio
    async def test_skips_non_copilot_prs(self) -> None:
        self_heal_issues = [_issue(100, labels=["caretaker:self-heal"])]
        human_pr = _pr(
            200,
            title="human PR",
            user=User(login="alice", id=99, type="User"),
            body="<!-- caretaker:task --> makes it 'managed'",
        )
        gh = make_github(issues=self_heal_issues, prs=[human_pr])

        await CharlieAgent(github=gh, owner="o", repo="r").run()

        gh.get_closing_issue_numbers.assert_not_awaited()
        gh.list_pull_request_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_dedupe_when_closing_issue_not_self_heal(self) -> None:
        issues = [_issue(50, labels=["maintainer:internal"])]
        pr_a = _pr(300, title="work A")
        pr_b = _pr(301, title="work B")
        gh = make_github(issues=issues, prs=[pr_a, pr_b])
        gh.get_closing_issue_numbers.side_effect = lambda owner, repo, n: [50]
        gh.list_pull_request_files.side_effect = lambda owner, repo, n: [
            {"path": "src/caretaker/foo.py", "additions": 10, "deletions": 0},
        ]

        report = await CharlieAgent(github=gh, owner="o", repo="r").run()

        # No self-heal label on closing issue → no primary-file fingerprint applied
        assert report.duplicate_prs_closed == 0
