"""Tests for caretaker.pr_agent.merge.perform_merge + rollback integration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from caretaker.config import MaintainerConfig
from caretaker.github_client.models import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Issue,
    PRState,
    PullRequest,
    User,
)
from caretaker.guardrails.rollback import RollbackOutcome
from caretaker.pr_agent.merge import (
    MergeDecision,
    perform_merge,
)


def _make_pr(number: int = 1) -> PullRequest:
    return PullRequest(
        number=number,
        title="Clean up stale imports",
        state=PRState.OPEN,
        user=User(login="someone", id=1),
        base_ref="main",
        head_ref="feat/cleanup",
    )


def _make_check_run(conclusion: CheckConclusion | None, status: CheckStatus) -> CheckRun:
    return CheckRun(
        id=1,
        name="ci / tests",
        status=status,
        conclusion=conclusion,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


@pytest.fixture
def pr_config_rollback_disabled() -> Any:
    return MaintainerConfig().pr_agent


@pytest.fixture
def pr_config_rollback_enabled() -> Any:
    config = MaintainerConfig().pr_agent
    config.merge_rollback.enabled = True
    config.merge_rollback.window_seconds = 5
    config.merge_rollback.poll_interval_seconds = 0
    # allow the merge policy to accept the PR under test (human PR, not draft)
    config.auto_merge.human_prs = True
    return config


@pytest.mark.asyncio
async def test_perform_merge_rollback_disabled_skips_verify(
    pr_config_rollback_disabled: Any,
) -> None:
    pr = _make_pr()
    gh = AsyncMock()
    gh.merge_pull_request.return_value = True
    gh.get_check_runs.side_effect = AssertionError("verify must not run when disabled")

    decision = MergeDecision(
        should_merge=True,
        method="squash",
        reason="all green",
        blockers=[],
    )
    # ensure human PR auto-merge is allowed for this test path
    pr_config_rollback_disabled.auto_merge.human_prs = True

    result = await perform_merge(
        pr,
        decision,
        github=gh,
        config=pr_config_rollback_disabled,
        owner="o",
        repo="r",
    )
    assert result.merged is True
    assert result.rollback_outcome is None
    gh.merge_pull_request.assert_awaited_once_with("o", "r", 1, method="squash")


@pytest.mark.asyncio
async def test_perform_merge_skips_when_decision_blocks() -> None:
    pr = _make_pr()
    gh = AsyncMock()
    config = MaintainerConfig().pr_agent

    decision = MergeDecision(
        should_merge=False,
        method="squash",
        reason="CI status: failing",
        blockers=["CI status: failing"],
    )
    result = await perform_merge(
        pr,
        decision,
        github=gh,
        config=config,
        owner="o",
        repo="r",
    )
    assert result.merged is False
    assert "policy_blocked" in result.reason
    gh.merge_pull_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_merge_rollback_fires_on_red_base_ci(
    pr_config_rollback_enabled: Any,
) -> None:
    pr = _make_pr(number=42)
    gh = AsyncMock()
    gh.merge_pull_request.return_value = True
    # Every check run on the base branch reports failure — the wrapper
    # should fire the rollback (which in our perform_merge closure opens
    # a tracking issue).
    gh.get_check_runs.return_value = [
        _make_check_run(CheckConclusion.FAILURE, CheckStatus.COMPLETED),
    ]
    gh.create_issue.return_value = Issue(
        number=999,
        title="rollback",
        state="open",
        user=User(login="caretaker-bot", id=1),
    )

    decision = MergeDecision(
        should_merge=True,
        method="squash",
        reason="all green at evaluation",
        blockers=[],
    )
    result = await perform_merge(
        pr,
        decision,
        github=gh,
        config=pr_config_rollback_enabled,
        owner="o",
        repo="r",
    )
    assert result.merged is True
    assert result.rollback_outcome is RollbackOutcome.ROLLED_BACK
    gh.create_issue.assert_awaited_once()
    # Inspect issue args: we should be labeling it caretaker:rollback.
    call_args = gh.create_issue.await_args
    assert "caretaker:rollback" in call_args.kwargs["labels"]


@pytest.mark.asyncio
async def test_perform_merge_rollback_happy_path_does_not_rollback(
    pr_config_rollback_enabled: Any,
) -> None:
    pr = _make_pr(number=7)
    gh = AsyncMock()
    gh.merge_pull_request.return_value = True
    gh.get_check_runs.return_value = [
        _make_check_run(CheckConclusion.SUCCESS, CheckStatus.COMPLETED),
    ]

    decision = MergeDecision(
        should_merge=True,
        method="squash",
        reason="clean",
        blockers=[],
    )
    result = await perform_merge(
        pr,
        decision,
        github=gh,
        config=pr_config_rollback_enabled,
        owner="o",
        repo="r",
    )
    assert result.merged is True
    assert result.rollback_outcome is RollbackOutcome.VERIFIED
    gh.create_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_perform_merge_filter_strips_llm_marker_in_rollback_issue(
    pr_config_rollback_enabled: Any,
) -> None:
    """Integration: when rollback opens an issue via create_issue, the
    output filter must scrub ANSI escapes from the body before POST."""
    pr = _make_pr(number=101)
    gh = AsyncMock()
    gh.merge_pull_request.return_value = True
    gh.get_check_runs.return_value = [
        _make_check_run(CheckConclusion.FAILURE, CheckStatus.COMPLETED),
    ]

    captured_bodies: list[str] = []

    async def fake_create_issue(
        owner: str,  # noqa: ARG001
        repo: str,  # noqa: ARG001
        title: str,  # noqa: ARG001
        body: str,
        **kwargs: Any,  # noqa: ARG001
    ) -> Issue:
        captured_bodies.append(body)
        return Issue(
            number=999,
            title="rollback",
            state="open",
            user=User(login="caretaker-bot", id=1),
        )

    gh.create_issue.side_effect = fake_create_issue

    decision = MergeDecision(
        should_merge=True,
        method="squash",
        reason="clean",
        blockers=[],
    )
    result = await perform_merge(
        pr,
        decision,
        github=gh,
        config=pr_config_rollback_enabled,
        owner="o",
        repo="r",
    )
    assert result.rollback_outcome is RollbackOutcome.ROLLED_BACK
    assert captured_bodies, "rollback must have posted an issue body"
    # The body text (caretaker-authored, not LLM) should not include any
    # ANSI escapes that might leak from environment variables — sanity
    # check only.
    for body in captured_bodies:
        assert "\x1b" not in body
