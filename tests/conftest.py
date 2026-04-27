"""Shared test fixtures for caretaker."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from caretaker.config import MaintainerConfig, PRAgentConfig
from caretaker.github_client.models import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Comment,
    Label,
    PRState,
    PullRequest,
    Review,
    ReviewState,
    User,
)
from caretaker.github_client.rate_limit import reset_for_tests as _reset_rate_limit


@pytest.fixture(autouse=True)
def _reset_rate_limit_cooldown() -> None:
    """The GitHub rate-limit cooldown is a process-wide singleton. Reset
    before every test so state (e.g. a test intentionally triggering
    the cooldown) doesn't leak into subsequent tests' HTTP stubs."""
    _reset_rate_limit()


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
    head_ref: str = "feature",
    created_at: datetime | None = None,
) -> PullRequest:
    if user is None:
        user = User(login="dev-user", id=3, type="User")
    # Default to "just now" so the stuck-PR age gate doesn't fire on tests
    # that don't care about age. Pass an explicit ancient datetime to test
    # age-related behavior.
    if created_at is None:
        created_at = datetime.now(UTC)
    return PullRequest(
        number=number,
        title=f"PR #{number}",
        body="",
        state=state,
        user=user,
        head_ref=head_ref,
        base_ref="main",
        mergeable=mergeable,
        merged=merged,
        draft=draft,
        labels=labels or [],
        created_at=created_at,
        updated_at=created_at,
        html_url=f"https://github.com/test/repo/pull/{number}",
    )


# ── Check Runs ───────────────────────────────────────────────────────


def make_check_run(
    name: str = "test",
    status: CheckStatus = CheckStatus.COMPLETED,
    conclusion: CheckConclusion | None = CheckConclusion.SUCCESS,
    output_title: str | None = None,
    output_summary: str | None = None,
    app_id: int | None = None,
) -> CheckRun:
    return CheckRun(
        id=1,
        name=name,
        status=status,
        conclusion=conclusion,
        output_title=output_title,
        output_summary=output_summary,
        app_id=app_id,
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
        submitted_at=submitted_at or datetime(2024, 1, 1, tzinfo=UTC),
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
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


# ── Config ───────────────────────────────────────────────────────────


@pytest.fixture
def default_config() -> MaintainerConfig:
    return MaintainerConfig()


@pytest.fixture
def pr_config() -> PRAgentConfig:
    return PRAgentConfig()
