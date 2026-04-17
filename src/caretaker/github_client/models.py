"""GitHub data models."""

# ruff: noqa: I001

from __future__ import annotations

import datetime as dt  # noqa: TC003

from enum import StrEnum

from pydantic import BaseModel, Field


COPILOT_LOGINS = frozenset(
    {
        "copilot",
        "github-copilot[bot]",
        "copilot[bot]",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
    }
)


def is_copilot_login(login: str) -> bool:
    """Return whether *login* refers to a GitHub Copilot coding agent identity."""
    return login.casefold() in COPILOT_LOGINS


class PRState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CheckStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    # Additional statuses returned by GitHub for checks awaiting a runner or gate
    WAITING = "waiting"
    REQUESTED = "requested"
    PENDING = "pending"


class CheckConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    STALE = "stale"


class ReviewState(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"
    PENDING = "PENDING"


class User(BaseModel):
    login: str
    id: int
    type: str = "User"


class Label(BaseModel):
    name: str
    color: str = ""


class CheckRun(BaseModel):
    id: int
    name: str
    status: CheckStatus
    conclusion: CheckConclusion | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    html_url: str = ""
    output_title: str | None = None
    output_summary: str | None = None


class Review(BaseModel):
    id: int
    user: User
    state: ReviewState
    body: str = ""
    submitted_at: dt.datetime | None = None


class Comment(BaseModel):
    id: int
    user: User
    body: str
    created_at: dt.datetime
    updated_at: dt.datetime | None = None

    @property
    def is_maintainer_task(self) -> bool:
        return "<!-- caretaker:task -->" in self.body

    @property
    def is_maintainer_result(self) -> bool:
        return "<!-- caretaker:result -->" in self.body


class PullRequest(BaseModel):
    number: int
    title: str
    body: str = ""
    state: PRState
    user: User
    head_ref: str = ""
    base_ref: str = ""
    mergeable: bool | None = None
    merged: bool = False
    draft: bool = False
    labels: list[Label] = Field(default_factory=list)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    merged_at: dt.datetime | None = None
    html_url: str = ""

    @property
    def is_copilot_pr(self) -> bool:
        return is_copilot_login(self.user.login)

    @property
    def is_dependabot_pr(self) -> bool:
        return self.user.login in ("dependabot[bot]", "dependabot")

    @property
    def is_maintainer_pr(self) -> bool:
        return any(lbl.name.startswith("maintainer:") for lbl in self.labels)

    def has_label(self, name: str) -> bool:
        return any(lbl.name == name for lbl in self.labels)


class Issue(BaseModel):
    number: int
    title: str
    body: str = ""
    state: str = "open"
    user: User
    labels: list[Label] = Field(default_factory=list)
    assignees: list[User] = Field(default_factory=list)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    html_url: str = ""

    def has_label(self, name: str) -> bool:
        return any(lbl.name == name for lbl in self.labels)

    @property
    def is_copilot_assigned(self) -> bool:
        return any(is_copilot_login(assignee.login) for assignee in self.assignees)

    @property
    def is_maintainer_issue(self) -> bool:
        return (
            self.title.startswith("[Maintainer]")
            or self.has_label("maintainer:internal")
            or self.has_label("maintainer:assigned")
            or "<!-- caretaker:assignment -->" in self.body
            or "<!-- maintainer-state:" in self.body
        )


class Repository(BaseModel):
    owner: str
    name: str
    full_name: str
    default_branch: str = "main"
    private: bool = False

    @property
    def nwo(self) -> str:
        """Name with owner (e.g. 'owner/repo')."""
        return self.full_name
