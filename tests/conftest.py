"""Shared test fixtures for caretaker."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from caretaker.config import MaintainerConfig, PRAgentConfig
from caretaker.github_client.models import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Comment,
    Label,
    PullRequest,
    PRState,
    Review,
    ReviewState,
    User,
)


# ── Users ────────────────────────────────────────────────────────────

@pytest.fixture
def copilot_user() -> User:
    return User(login="copilot[bot]", id=1, type="Bot")


@pytest.fixture
def dependabot_user() -> User:
    return User(login="dependabot[bot]", id=2, type="Bot")


@pytest.fixture
def human_user() -> User:
    return User(login="dev-user", id=3, type="User")


@pytest.fixture
def reviewer_user() -> User:
    return User(login="reviewer", id=4, type="User")


# ── Pull Requests ────────────────────────────────────────────────────

def make_pr(
    number: int = 1,
    user: User | None = None,
    state: PRState = PRState.OPEN,
    labels: list[Label] | None = None,
    draft: bool = False,
    merged: bool = False,
    mergeable: bool | None = True,
) -> PullRequest:
    if user is None:
        user = User(login="dev-user", id=3, type="User")
    return PullRequest(
        number=number,
        title=f"PR #{number}",
        body="",
        state=state,
        user=user,
        head_ref="feature",
        base_ref="main",
        mergeable=mergeable,
        merged=merged,
        draft=draft,
        labels=labels or [],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        html_url=f"https://github.com/test/repo/pull/{number}",
    )


# ── Check Runs ───────────────────────────────────────────────────────

def make_check_run(
    name: str = "test",
    status: CheckStatus = CheckStatus.COMPLETED,
    conclusion: CheckConclusion | None = CheckConclusion.SUCCESS,
    output_title: str | None = None,
    output_summary: str | None = None,
) -> CheckRun:
    return CheckRun(
        id=1,
        name=name,
        status=status,
        conclusion=conclusion,
        output_title=output_title,
        output_summary=output_summary,
    )


# ── Reviews ──────────────────────────────────────────────────────────

def make_review(
    user: User | None = None,
    state: ReviewState = ReviewState.APPROVED,
    body: str = "",
    submitted_at: datetime | None = None,
) -> Review:
    if user is None:
        user = User(login="reviewer", id=4, type="User")
    return Review(
        id=1,
        user=user,
        state=state,
        body=body,
        submitted_at=submitted_at or datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# ── Comments ─────────────────────────────────────────────────────────

def make_comment(
    body: str = "",
    user: User | None = None,
) -> Comment:
    if user is None:
        user = User(login="bot", id=99, type="Bot")
    return Comment(
        id=1,
        user=user,
        body=body,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


# ── Config ───────────────────────────────────────────────────────────

@pytest.fixture
def default_config() -> MaintainerConfig:
    return MaintainerConfig()


@pytest.fixture
def pr_config() -> PRAgentConfig:
    return PRAgentConfig()
