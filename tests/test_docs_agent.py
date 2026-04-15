"""Tests for the docs agent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from caretaker.docs_agent.agent import (
    DOCS_AGENT_MARKER,
    DocsAgent,
    _build_changelog_entry,
    _build_copilot_review_comment,
    _clean_title,
)
from caretaker.github_client.api import GitHubAPIError
from caretaker.github_client.models import PullRequest, User


def _pr(number: int, title: str, merged_at: str | None = None) -> SimpleNamespace:
    """Return a lightweight PR-like object that DocsAgent can iterate over."""
    return SimpleNamespace(
        number=number,
        title=title,
        body="",
        merged_at=merged_at,  # attribute checked by _get_recently_merged_prs
        labels=[],
        user=User(login="dev", id=1),
    )


def _open_docs_pr(number: int = 55) -> PullRequest:
    """Return an open PR that looks like a docs-update PR (has the marker)."""
    return PullRequest(
        number=number,
        title="docs: reconcile CHANGELOG",
        body=f"{DOCS_AGENT_MARKER}\n-->",
        state="open",
        user=User(login="bot", id=9),
        head_ref="docs/changelog-2024-W01",
        base_ref="main",
    )


def make_github() -> AsyncMock:
    gh = AsyncMock()
    gh.list_pull_requests.return_value = []
    gh.get_file_contents.return_value = None
    gh.ensure_label.return_value = None
    gh._get.return_value = {"object": {"sha": "abc123sha"}}
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
            _pr(1, "feat: add search"),
            _pr(2, "fix: broken login"),
        ]
        entry = _build_changelog_entry(prs)
        assert "add search" in entry
        assert "broken login" in entry
        assert "#1" in entry
        assert "#2" in entry

    def test_strips_conventional_prefix_in_entry(self) -> None:
        prs = [_pr(3, "feat: shiny new thing")]
        entry = _build_changelog_entry(prs)
        # prefix stripped; raw "feat:" should not appear
        assert "feat:" not in entry
        assert "shiny new thing" in entry


# ── DocsAgent integration tests ─────────────────────────────────────


class TestDocsAgentRun:
    @pytest.mark.asyncio
    async def test_opens_pr_when_merged_prs_found(self) -> None:
        merged = [_pr(10, "feat: cool feature", merged_at="2024-01-10T12:00:00+00:00")]
        gh = make_github()
        agent = DocsAgent(github=gh, owner="o", repo="r", default_branch="main")

        with (
            patch.object(agent, "_get_recently_merged_prs", return_value=merged),
            patch.object(agent, "_find_open_docs_prs", return_value=[]),
        ):
            report = await agent.run()

        assert report.prs_analyzed == 1
        assert report.doc_pr_opened == 77
        gh.create_pull_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_assigns_copilot_to_pr(self) -> None:
        merged = [_pr(10, "fix: something", merged_at="2024-01-10T12:00:00+00:00")]
        gh = make_github()
        agent = DocsAgent(github=gh, owner="o", repo="r")

        with (
            patch.object(agent, "_get_recently_merged_prs", return_value=merged),
            patch.object(agent, "_find_open_docs_prs", return_value=[]),
        ):
            await agent.run()

        call_kwargs = gh.create_pull_request.call_args.kwargs
        assert "copilot" in call_kwargs.get("assignees", [])

    @pytest.mark.asyncio
    async def test_posts_follow_up_copilot_comment_with_pat_identity(self) -> None:
        merged = [_pr(10, "fix: something", merged_at="2024-01-10T12:00:00+00:00")]
        gh = make_github()
        agent = DocsAgent(github=gh, owner="o", repo="r")

        with (
            patch.object(agent, "_get_recently_merged_prs", return_value=merged),
            patch.object(agent, "_find_open_docs_prs", return_value=[]),
        ):
            await agent.run()

        gh.add_issue_comment.assert_awaited_once_with(
            "o",
            "r",
            77,
            _build_copilot_review_comment(),
            use_copilot_token=True,
        )

    @pytest.mark.asyncio
    async def test_skips_when_no_merged_prs(self) -> None:
        gh = make_github()
        agent = DocsAgent(github=gh, owner="o", repo="r")

        with patch.object(agent, "_get_recently_merged_prs", return_value=[]):
            report = await agent.run()

        assert report.prs_analyzed == 0
        assert report.doc_pr_opened is None
        gh.create_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_open_docs_pr_exists(self) -> None:
        merged = [_pr(10, "feat: cool feature", merged_at="2024-01-10T12:00:00+00:00")]
        gh = make_github()
        agent = DocsAgent(github=gh, owner="o", repo="r")

        with (
            patch.object(agent, "_get_recently_merged_prs", return_value=merged),
            patch.object(agent, "_find_open_docs_prs", return_value=[55]),
        ):
            report = await agent.run()

        # should return the existing PR number, not create a new one
        assert report.doc_pr_opened == 55
        gh.create_pull_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reuses_existing_branch_when_422(self) -> None:
        """If the branch already exists (422), the agent reuses it and still opens the PR."""
        merged = [_pr(10, "feat: cool feature", merged_at="2024-01-10T12:00:00+00:00")]
        gh = make_github()
        # Simulate branch already existing
        gh.create_branch.side_effect = GitHubAPIError(422, '{"message":"Reference already exists"}')
        agent = DocsAgent(github=gh, owner="o", repo="r", default_branch="main")

        with (
            patch.object(agent, "_get_recently_merged_prs", return_value=merged),
            patch.object(agent, "_find_open_docs_prs", return_value=[]),
        ):
            report = await agent.run()

        # No error reported — branch reuse is transparent
        assert report.errors == []
        assert report.doc_pr_opened == 77
        # The file should still be committed and the PR created
        gh.create_or_update_file.assert_awaited_once()
        gh.create_pull_request.assert_awaited_once()
