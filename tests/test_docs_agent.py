"""Tests for the docs agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.docs_agent.agent import DocsAgent, _build_changelog_entry, _clean_title
from caretaker.github_client.models import PullRequest, User


def _merged_pr(
    number: int,
    title: str,
    merged_days_ago: int = 1,
) -> PullRequest:
    merged_at = (datetime.now(timezone.utc) - timedelta(days=merged_days_ago)).isoformat()
    pr = PullRequest(
        number=number,
        title=title,
        body="",
        state="closed",
        user=User(login="dev", id=1),
        head_ref="feature",
        base_ref="main",
        mergeable=None,
        merged=True,
        draft=False,
        labels=[],
        html_url=f"https://github.com/o/r/pull/{number}",
        created_at=merged_at,
        updated_at=merged_at,
    )
    # DocsAgent checks the raw PR for merged_at; inject it via additional attribute
    object.__setattr__(pr, "_merged_at_raw", merged_at)
    return pr


def make_github(
    prs: list | None = None,
    file_contents: dict | None = None,
) -> AsyncMock:
    gh = AsyncMock()
    gh.list_pull_requests.return_value = prs or []
    gh.get_file_contents.return_value = file_contents  # None → file doesn't exist yet
    gh.ensure_label.return_value = None
    gh.get_default_branch_sha.return_value = "abc123"
    gh.create_branch.return_value = None
    gh.create_or_update_file.return_value = {"content": {"sha": "def456"}}
    gh.create_pull_request.return_value = {"number": 77}
    return gh


# ── Helper function tests ────────────────────────────────────────────


class TestCleanTitle:
    def test_strips_feat_prefix(self) -> None:
        assert _clean_title("feat: add new feature") == "add new feature"

    def test_strips_fix_prefix(self) -> None:
        assert _clean_title("fix(auth): resolve login bug") == "resolve login bug"

    def test_strips_breaking_change(self) -> None:
        assert _clean_title("feat!: breaking new thing") == "breaking new thing"

    def test_leaves_plain_title_unchanged(self) -> None:
        assert _clean_title("Update README") == "Update README"

    def test_strips_chore_prefix(self) -> None:
        assert _clean_title("chore: bump version") == "bump version"


class TestBuildChangelogEntry:
    def test_produces_markdown_with_pr_links(self) -> None:
        prs = [
            _merged_pr(1, "feat: add search"),
            _merged_pr(2, "fix: broken login"),
        ]
        entry = _build_changelog_entry(prs, "o", "r")
        assert "## [Unreleased]" in entry
        assert "add search" in entry
        assert "broken login" in entry
        assert "#1" in entry
        assert "#2" in entry


# ── DocsAgent integration tests ─────────────────────────────────────


class TestDocsAgentRun:
    @pytest.mark.asyncio
    async def test_opens_pr_when_merged_prs_exist(self) -> None:
        prs = [_merged_pr(10, "feat: cool feature")]
        gh = make_github(prs=prs)
        agent = DocsAgent(github=gh, owner="o", repo="r", default_branch="main")

        with patch.object(
            agent, "_get_merged_prs_since", return_value=prs
        ), patch.object(
            agent, "_duplicate_doc_pr_exists", return_value=False
        ):
            report = await agent.run()

        assert report.prs_analyzed == 1
        assert report.doc_pr_opened == 77
        gh.create_pull_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_merged_prs(self) -> None:
        gh = make_github(prs=[])
        agent = DocsAgent(github=gh, owner="o", repo="r", default_branch="main")

        with patch.object(agent, "_get_merged_prs_since", return_value=[]):
            report = await agent.run()

        assert report.prs_analyzed == 0
        assert report.doc_pr_opened is None
        gh.create_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_duplicate_pr_open(self) -> None:
        prs = [_merged_pr(10, "feat: cool feature")]
        gh = make_github(prs=prs)
        agent = DocsAgent(github=gh, owner="o", repo="r", default_branch="main")

        with patch.object(
            agent, "_get_merged_prs_since", return_value=prs
        ), patch.object(
            agent, "_duplicate_doc_pr_exists", return_value=True
        ):
            report = await agent.run()

        assert report.doc_pr_opened is None
        gh.create_pull_request.assert_not_awaited()
