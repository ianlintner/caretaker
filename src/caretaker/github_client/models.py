"""GitHub data models."""

# ruff: noqa: I001

from __future__ import annotations

import datetime as dt  # noqa: TC003

from enum import StrEnum

from pydantic import BaseModel, Field

from caretaker.identity import deterministic_family

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
    """Return whether *login* refers to a GitHub Copilot coding agent identity.

    Delegates to :func:`caretaker.identity.deterministic_family` so the
    Copilot allowlist stays consolidated in one place.
    """
    return deterministic_family(login.casefold()) == "copilot"


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
    PENDING = "pending"
    REQUESTED = "requested"


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


class MergeStateStatus(StrEnum):
    """GraphQL ``mergeStateStatus`` values surfaced for shepherd routing.

    ``mergeable`` on the REST API is a tri-state bool and conflates distinct
    failure modes the shepherd needs to distinguish:

    * ``BEHIND``  — branch is up-to-date-free but lags base; needs
      ``update-branch`` (the shepherd's cascade-handling case).
    * ``DIRTY``   — real merge conflict; needs rebase or stale-reap.
    * ``BLOCKED`` — required reviews or status checks aren't satisfied.
    * ``UNSTABLE`` — mergeable but at least one non-required check is
      failing (common intermediate state while CI re-runs).
    * ``HAS_HOOKS`` — mergeable after required hooks pass.
    * ``CLEAN``   — fully mergeable.
    * ``UNKNOWN`` — GitHub hasn't finished computing mergeability.

    Populated by the shepherd via a targeted GraphQL lookup because the
    REST API does not return this field on the PR list endpoint.
    """

    BEHIND = "BEHIND"
    DIRTY = "DIRTY"
    BLOCKED = "BLOCKED"
    UNSTABLE = "UNSTABLE"
    HAS_HOOKS = "HAS_HOOKS"
    CLEAN = "CLEAN"
    UNKNOWN = "UNKNOWN"


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
    # The GitHub App id that *created* this check run.  Populated from the
    # ``app.id`` field in the API response.  Used to detect cross-App
    # ownership conflicts before attempting an update (GitHub returns 403
    # "Invalid app_id" when an update is attempted by a different App).
    app_id: int | None = None


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
    # GitHub author_association values: OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR,
    # FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, NONE.  Used by _apply_merge_command
    # to gate the @caretaker merge command to trusted collaborators only.
    author_association: str | None = None

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
    head_sha: str = ""
    base_ref: str = ""
    # Full ``owner/repo`` of the PR's head and base. When the PR is from a
    # fork the two differ; caretaker uses this to guard against writes that
    # an App installation token cannot perform on the fork.
    head_repo_full_name: str = ""
    base_repo_full_name: str = ""
    mergeable: bool | None = None
    # GraphQL-only field — distinguishes BEHIND (needs update-branch) from
    # DIRTY (needs rebase) from BLOCKED (needs review/CI). The REST PR list
    # endpoint does not populate this; shepherd enriches via GraphQL.
    # ``None`` means the field was never fetched (legacy callers); callers
    # should treat ``None`` as "unknown, skip shepherd routing".
    merge_state_status: MergeStateStatus | None = None
    merged: bool = False
    draft: bool = False
    # GitHub's global node ID — required for GraphQL mutations such as
    # markPullRequestReadyForReview.  Populated from the REST ``node_id``
    # field; empty string when fetched via older code paths that pre-date
    # this field.
    node_id: str = ""
    labels: list[Label] = Field(default_factory=list)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    merged_at: dt.datetime | None = None
    html_url: str = ""

    @property
    def is_fork(self) -> bool:
        """Return True when the PR originates from a fork.

        Defaults to False when the repo identity is unknown so the PR-agent
        path behaves exactly as it did before these fields were added.
        """
        if not self.head_repo_full_name or not self.base_repo_full_name:
            return False
        return self.head_repo_full_name != self.base_repo_full_name

    @property
    def is_copilot_pr(self) -> bool:
        return is_copilot_login(self.user.login)

    @property
    def is_dependabot_pr(self) -> bool:
        return deterministic_family(self.user.login) == "dependabot"

    @property
    def is_caretaker_pr(self) -> bool:
        """Return True for PRs authored by the caretaker agent itself.

        Identified by head-branch prefix: ``claude/`` (Claude Code sessions)
        or ``caretaker/`` (dedicated caretaker agent branches).
        """
        return self.head_ref.startswith(("claude/", "caretaker/"))

    @property
    def is_maintainer_bot_pr(self) -> bool:
        """Return True for automated maintenance PRs created by caretaker workflows.

        These are PRs opened by GitHub Actions on behalf of caretaker's own
        release/maintenance workflows (e.g. ``update-releases-json.yml``).
        They are safe to auto-approve and auto-merge once CI passes — they
        contain only mechanical, workflow-generated changes (e.g. releases.json
        entries) with no human-authored code.

        Identified by:
        - Head-branch prefix ``chore/releases-json-`` (update-releases-json workflow), OR
        - Author login ``github-actions[bot]`` combined with a ``chore/`` branch prefix
          (future-proofs additional chore workflows without requiring per-workflow
          changes here).
        """
        return self.head_ref.startswith("chore/releases-json-") or (
            self.user.login in ("github-actions[bot]", "github-actions")
            and self.head_ref.startswith("chore/")
        )

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
            or self.title.startswith("[Caretaker]")
            or self.has_label("maintainer:internal")
            or self.has_label("maintainer:assigned")
            or self.has_label("maintainer:escalation-digest")
            or "<!-- caretaker:assignment -->" in self.body
            or "<!-- maintainer-state:" in self.body
            or "<!-- caretaker:escalation-digest" in self.body
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
