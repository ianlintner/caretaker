"""Tests for the dependency agent."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from caretaker.dependency_agent.agent import (
    DependencyAgent,
    DependencyBump,
    _detect_ecosystem,
    _is_major_bump,
    _parse_bump,
)
from caretaker.github_client.models import Issue, Label, PullRequest, User


def make_pr(
    number: int = 1,
    title: str = "Bump requests from 2.28.0 to 2.29.0",
    user_login: str = "dependabot[bot]",
    merged_at: str | None = None,
    labels: list[Label] | None = None,
) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        body="",
        state="open",
        user=User(login=user_login, id=2),
        head_ref=f"dependabot/pip/requests-2.29.0",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=False,
        labels=labels or [],
        html_url=f"https://github.com/o/r/pull/{number}",
    )


def make_github(
    prs: list | None = None,
    ci_status: str = "success",
    existing_issues: list | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_pull_requests.return_value = prs or []
    gh.get_combined_status.return_value = ci_status
    gh.merge_pull_request.return_value = True
    gh.ensure_label.return_value = None
    gh.list_issues.return_value = existing_issues or []
    gh.create_issue.return_value = Issue(
        number=99,
        title="",
        body="",
        state="open",
        user=User(login="bot", id=1),
        labels=[],
        assignees=[],
        html_url="",
    )
    gh.update_issue.return_value = Issue(
        number=99,
        title="",
        body="",
        state="open",
        user=User(login="bot", id=1),
        labels=[],
        assignees=[],
        html_url="",
    )
    return gh


# ── Helper function tests ────────────────────────────────────────────


class TestParseBump:
    def test_parses_standard_title(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.29.0")
        result = _parse_bump(pr)
        assert result is not None
        assert result.package == "requests"
        assert result.from_version == "2.28.0"
        assert result.to_version == "2.29.0"

    def test_parses_scoped_package(self) -> None:
        pr = make_pr(title="Bump @babel/core from 7.0.0 to 8.0.0")
        result = _parse_bump(pr)
        assert result is not None
        assert result.package == "@babel/core"

    def test_returns_none_for_non_dependabot_title(self) -> None:
        pr = make_pr(title="fix: update login logic")
        assert _parse_bump(pr) is None

    def test_parses_pre_release_version(self) -> None:
        pr = make_pr(title="Bump pytest from 8.0.0a1 to 8.0.0")
        assert _parse_bump(pr) is not None


class TestIsMajorBump:
    def test_major_bump(self) -> None:
        assert _is_major_bump("1.5.0", "2.0.0") is True

    def test_minor_bump(self) -> None:
        assert _is_major_bump("1.5.0", "1.6.0") is False

    def test_patch_bump(self) -> None:
        assert _is_major_bump("1.5.0", "1.5.1") is False

    def test_zero_major_bump(self) -> None:
        assert _is_major_bump("0.9.0", "1.0.0") is True

    def test_malformed_versions(self) -> None:
        # Should not raise, returns False for unparseable
        assert _is_major_bump("abc", "def") is False


class TestDetectEcosystem:
    def test_detects_pip(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.29.0",
                     number=1)
        # head_ref from make_pr is "dependabot/pip/requests-2.29.0" by default
        assert _detect_ecosystem(pr) == "pip"

    def test_detects_npm(self) -> None:
        from caretaker.github_client.models import PullRequest, User
        pr = PullRequest(
            number=2,
            title="Bump lodash from 4.17.20 to 4.17.21",
            body="",
            state="open",
            user=User(login="dependabot[bot]", id=2),
            head_ref="dependabot/npm_and_yarn/lodash-4.17.21",
            base_ref="main",
            mergeable=True,
            merged=False,
            draft=False,
            labels=[],
            html_url="https://github.com/o/r/pull/2",
        )
        assert _detect_ecosystem(pr) == "npm"

    def test_returns_unknown(self) -> None:
        from caretaker.github_client.models import PullRequest, User
        pr = PullRequest(
            number=3,
            title="Update feature flags",
            body="",
            state="open",
            user=User(login="human", id=3),
            head_ref="feature/my-feature",
            base_ref="main",
            mergeable=True,
            merged=False,
            draft=False,
            labels=[],
            html_url="https://github.com/o/r/pull/3",
        )
        assert _detect_ecosystem(pr) == "unknown"


# ── DependencyAgent tests ────────────────────────────────────────────


class TestDependencyAgentAutoMerge:
    @pytest.mark.asyncio
    async def test_auto_merges_patch_bump(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.28.1")
        gh = make_github(prs=[pr], ci_status="success")
        agent = DependencyAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.prs_reviewed == 1
        assert len(report.prs_auto_merged) == 1
        gh.merge_pull_request.assert_awaited_once_with("o", "r", pr.number, method="squash")

    @pytest.mark.asyncio
    async def test_auto_merges_minor_bump(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.29.0")
        gh = make_github(prs=[pr], ci_status="success")
        agent = DependencyAgent(github=gh, owner="o", repo="r", auto_merge_minor=True)
        report = await agent.run()

        assert len(report.prs_auto_merged) == 1

    @pytest.mark.asyncio
    async def test_skips_merge_when_ci_failing(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.28.1")
        gh = make_github(prs=[pr], ci_status="failure")
        agent = DependencyAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert len(report.prs_auto_merged) == 0
        gh.merge_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_issue_for_major_bump(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 3.0.0")
        gh = make_github(prs=[pr])
        agent = DependencyAgent(github=gh, owner="o", repo="r", post_digest=False)
        report = await agent.run()

        assert len(report.major_issues_created) == 1
        gh.create_issue.assert_awaited_once()
        call_kwargs = gh.create_issue.call_args.kwargs
        assert "copilot" in call_kwargs.get("assignees", [])

    @pytest.mark.asyncio
    async def test_ignores_non_dependabot_prs(self) -> None:
        pr = make_pr(user_login="human-dev", title="Bump requests from 2.28.0 to 2.28.1")
        gh = make_github(prs=[pr])
        agent = DependencyAgent(github=gh, owner="o", repo="r")
        report = await agent.run()

        assert report.prs_reviewed == 0
        gh.merge_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_merge_when_patch_disabled(self) -> None:
        pr = make_pr(title="Bump requests from 2.28.0 to 2.28.1")
        gh = make_github(prs=[pr], ci_status="success")
        agent = DependencyAgent(
            github=gh, owner="o", repo="r",
            auto_merge_patch=False, auto_merge_minor=False,
        )
        report = await agent.run()

        assert len(report.prs_auto_merged) == 0
