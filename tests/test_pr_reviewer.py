"""Tests for the PR reviewer agent — routing, inline review, and claude-code hand-off."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.config import PRReviewerConfig
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult
from caretaker.pr_reviewer.routing import decide

# ── routing tests ──────────────────────────────────────────────────────────


def test_routing_low_complexity() -> None:
    d = decide(
        additions=20,
        deletions=10,
        file_count=2,
        file_paths=["src/foo.py", "tests/test_foo.py"],
        pr_labels=[],
        threshold=40,
    )
    assert d.use_inline is True
    assert d.score < 40


def test_routing_high_loc_forces_claude_code() -> None:
    # High LOC (30pts) + many files (20pts) pushes score above threshold
    d = decide(
        additions=900,
        deletions=200,
        file_count=25,
        file_paths=[f"src/module{i}.py" for i in range(25)],
        pr_labels=[],
        threshold=40,
    )
    assert d.use_inline is False
    assert d.score >= 40


def test_routing_sensitive_file_bumps_score() -> None:
    d = decide(
        additions=10,
        deletions=5,
        file_count=1,
        file_paths=[".github/workflows/ci.yml"],
        pr_labels=[],
        threshold=40,
    )
    assert d.score >= 15


def test_routing_complex_label_bumps_score() -> None:
    baseline = decide(
        additions=10,
        deletions=5,
        file_count=1,
        file_paths=["src/foo.py"],
        pr_labels=[],
        threshold=40,
    )
    labeled = decide(
        additions=10,
        deletions=5,
        file_count=1,
        file_paths=["src/foo.py"],
        pr_labels=["architecture"],
        threshold=40,
    )
    assert labeled.score > baseline.score


def test_routing_simple_label_reduces_score() -> None:
    baseline = decide(
        additions=60,
        deletions=10,
        file_count=2,
        file_paths=["README.md"],
        pr_labels=[],
        threshold=40,
    )
    labeled = decide(
        additions=60,
        deletions=10,
        file_count=2,
        file_paths=["README.md"],
        pr_labels=["docs"],
        threshold=40,
    )
    assert labeled.score <= baseline.score


def test_routing_many_dirs_architecture_signal() -> None:
    paths = [f"pkg{i}/module.py" for i in range(8)]
    d = decide(
        additions=30,
        deletions=10,
        file_count=8,
        file_paths=paths,
        pr_labels=[],
        threshold=40,
    )
    assert d.score >= 15  # arch signal contributed


def test_routing_score_capped_at_100() -> None:
    d = decide(
        additions=2000,
        deletions=1000,
        file_count=50,
        file_paths=[f".github/workflows/job{i}.yml" for i in range(10)],
        pr_labels=["architecture", "migration"],
        threshold=40,
    )
    assert d.score <= 100


# ── inline reviewer tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_review_success() -> None:
    import json

    mock_response = json.dumps(
        {
            "summary": "Looks good overall.",
            "verdict": "APPROVE",
            "comments": [
                {"path": "src/foo.py", "line": 10, "body": "Consider a docstring here."}
            ],
        }
    )

    mock_llm = MagicMock()
    mock_llm.available = True
    mock_llm.claude = MagicMock()
    mock_llm.claude.complete = AsyncMock(return_value=mock_response)

    mock_github = MagicMock()
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y"
    mock_github.get_pull_diff = AsyncMock(return_value=diff)

    from caretaker.pr_reviewer.inline_reviewer import review

    result = await review(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=42,
        pr_title="Fix foo",
        pr_body="Fixes the foo bug",
        llm=mock_llm,
    )

    assert result.verdict == "APPROVE"
    assert "Looks good" in result.summary
    assert len(result.comments) == 1
    assert result.comments[0].path == "src/foo.py"


@pytest.mark.asyncio
async def test_inline_review_empty_diff() -> None:
    mock_llm = MagicMock()
    mock_github = MagicMock()
    mock_github.get_pull_diff = AsyncMock(return_value="")

    from caretaker.pr_reviewer.inline_reviewer import review

    result = await review(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=1,
        pr_title="Empty",
        pr_body="",
        llm=mock_llm,
    )
    assert result.verdict == "COMMENT"
    assert "Could not fetch diff" in result.summary


@pytest.mark.asyncio
async def test_inline_review_llm_failure() -> None:
    mock_llm = MagicMock()
    mock_llm.claude = MagicMock()
    mock_llm.claude.complete = AsyncMock(side_effect=RuntimeError("timeout"))

    mock_github = MagicMock()
    mock_github.get_pull_diff = AsyncMock(return_value="diff content")

    from caretaker.pr_reviewer.inline_reviewer import review

    result = await review(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=2,
        pr_title="Foo",
        pr_body="",
        llm=mock_llm,
    )
    assert result.verdict == "COMMENT"
    assert "failed" in result.summary.lower()


# ── claude-code hand-off tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_code_dispatch_success() -> None:
    mock_github = MagicMock()
    mock_github.ensure_label = AsyncMock()
    mock_github.add_labels = AsyncMock(return_value=[])
    mock_github.upsert_issue_comment = AsyncMock()

    cfg = PRReviewerConfig(enabled=True)

    from caretaker.pr_reviewer.claude_code_reviewer import dispatch

    ok = await dispatch(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=5,
        config=cfg,
        routing_reason="score=55 [loc=400]",
    )
    assert ok is True
    mock_github.add_labels.assert_awaited_once()
    mock_github.upsert_issue_comment.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_code_dispatch_label_failure() -> None:
    mock_github = MagicMock()
    mock_github.ensure_label = AsyncMock()
    mock_github.add_labels = AsyncMock(side_effect=RuntimeError("403"))

    cfg = PRReviewerConfig(enabled=True)

    from caretaker.pr_reviewer.claude_code_reviewer import dispatch

    ok = await dispatch(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=6,
        config=cfg,
        routing_reason="score=60",
    )
    assert ok is False


# ── github_review tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_review_success() -> None:
    mock_github = MagicMock()
    mock_github.create_review = AsyncMock(return_value={"id": 1})

    result = ReviewResult(
        summary="LGTM",
        verdict="APPROVE",
        comments=[InlineReviewComment(path="foo.py", line=1, body="Nice")],
    )

    from caretaker.pr_reviewer.github_review import post_review

    await post_review(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=10,
        commit_sha="abc123",
        result=result,
    )
    mock_github.create_review.assert_awaited_once()
    call_kwargs = mock_github.create_review.call_args.kwargs
    assert call_kwargs["event"] == "APPROVE"
    assert len(call_kwargs["comments"]) == 1


@pytest.mark.asyncio
async def test_post_review_falls_back_on_error() -> None:
    mock_github = MagicMock()
    mock_github.create_review = AsyncMock(side_effect=RuntimeError("fail"))
    mock_github.upsert_issue_comment = AsyncMock()

    result = ReviewResult(summary="Something", verdict="COMMENT")

    from caretaker.pr_reviewer.github_review import post_review

    await post_review(
        github=mock_github,
        owner="org",
        repo="repo",
        pr_number=11,
        commit_sha="def456",
        result=result,
    )
    mock_github.upsert_issue_comment.assert_awaited_once()


# ── config defaults ────────────────────────────────────────────────────────


def test_pr_reviewer_config_defaults() -> None:
    cfg = PRReviewerConfig()
    assert cfg.enabled is False
    assert cfg.routing_threshold == 40
    assert cfg.skip_draft is True
    assert "caretaker:reviewed" in cfg.skip_labels
    assert cfg.review_event == "AUTO"


def test_maintainer_config_includes_pr_reviewer() -> None:
    from caretaker.config import MaintainerConfig

    mc = MaintainerConfig()
    assert hasattr(mc, "pr_reviewer")
    assert isinstance(mc.pr_reviewer, PRReviewerConfig)
    assert mc.pr_reviewer.enabled is False


# ── event routing ──────────────────────────────────────────────────────────


def test_events_route_pr_reviewer() -> None:
    from caretaker.github_app.events import agents_for_event

    agents = agents_for_event("pull_request")
    assert "pr-reviewer" in agents


def test_registry_includes_pr_reviewer() -> None:
    from caretaker.agents._registry_data import AGENT_MODES, ALL_ADAPTERS
    from caretaker.pr_reviewer.agent import PRReviewerAgent

    assert PRReviewerAgent in ALL_ADAPTERS
    assert "pr-reviewer" in AGENT_MODES
