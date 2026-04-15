"""Repo-bound GitHub tools used by caretaker agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import builtins

    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Comment, Issue, Label, PullRequest, Review


@dataclass(frozen=True, slots=True)
class CopilotAgentAssignment:
    """Optional routing metadata for a Copilot coding-agent task."""

    target_repo: str
    base_branch: str | None = None
    custom_instructions: str = ""
    custom_agent: str = ""
    model: str = ""

    def to_api_payload(self) -> dict[str, str]:
        payload = {"target_repo": self.target_repo}
        if self.base_branch:
            payload["base_branch"] = self.base_branch
        if self.custom_instructions:
            payload["custom_instructions"] = self.custom_instructions
        if self.custom_agent:
            payload["custom_agent"] = self.custom_agent
        if self.model:
            payload["model"] = self.model
        return payload


class GitHubIssueTools:
    """Repo-bound issue tools with explicit Copilot assignment helpers."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo

    @property
    def target_repo(self) -> str:
        return f"{self._owner}/{self._repo}"

    def default_copilot_assignment(
        self,
        *,
        base_branch: str | None = None,
        custom_instructions: str = "",
        custom_agent: str = "",
        model: str = "",
    ) -> CopilotAgentAssignment:
        return CopilotAgentAssignment(
            target_repo=self.target_repo,
            base_branch=base_branch,
            custom_instructions=custom_instructions,
            custom_agent=custom_agent,
            model=model,
        )

    async def list(self, state: str = "open", labels: str | None = None) -> builtins.list[Issue]:
        return await self._github.list_issues(self._owner, self._repo, state=state, labels=labels)

    async def create(
        self,
        title: str,
        body: str,
        labels: builtins.list[str] | None = None,
        assignees: builtins.list[str] | None = None,
        copilot_assignment: CopilotAgentAssignment | None = None,
    ) -> Issue:
        return await self._github.create_issue(
            owner=self._owner,
            repo=self._repo,
            title=title,
            body=body,
            labels=labels,
            assignees=assignees,
            copilot_assignment=copilot_assignment,
        )

    async def update(self, number: int, **kwargs: Any) -> Issue:
        return await self._github.update_issue(self._owner, self._repo, number, **kwargs)

    async def comment(
        self,
        number: int,
        body: str,
        *,
        use_copilot_token: bool | None = None,
    ) -> Comment:
        return await self._github.add_issue_comment(
            self._owner,
            self._repo,
            number,
            body,
            use_copilot_token=use_copilot_token,
        )

    async def add_labels(self, number: int, labels: builtins.list[str]) -> builtins.list[Label]:
        return await self._github.add_labels(self._owner, self._repo, number, labels)

    async def ensure_label(self, name: str, color: str, description: str = "") -> None:
        await self._github.ensure_label(self._owner, self._repo, name, color, description)

    async def assign_copilot(
        self,
        number: int,
        assignment: CopilotAgentAssignment | None = None,
    ) -> None:
        await self._github.assign_copilot_to_issue(
            self._owner,
            self._repo,
            number,
            assignment=assignment,
        )


class GitHubPullRequestTools:
    """Repo-bound pull request tools for common caretaker workflows."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo

    async def list(self, state: str = "open") -> builtins.list[PullRequest]:
        return await self._github.list_pull_requests(self._owner, self._repo, state=state)

    async def get(self, number: int) -> PullRequest | None:
        return await self._github.get_pull_request(self._owner, self._repo, number)

    async def create(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: builtins.list[str] | None = None,
        assignees: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._github.create_pull_request(
            owner=self._owner,
            repo=self._repo,
            title=title,
            body=body,
            head=head,
            base=base,
            labels=labels,
            assignees=assignees,
        )

    async def merge(self, number: int, method: str = "squash") -> bool:
        return await self._github.merge_pull_request(
            self._owner,
            self._repo,
            number,
            method=method,
        )

    async def comment(
        self,
        number: int,
        body: str,
        *,
        use_copilot_token: bool | None = None,
    ) -> Comment:
        return await self._github.add_issue_comment(
            self._owner,
            self._repo,
            number,
            body,
            use_copilot_token=use_copilot_token,
        )

    async def add_labels(self, number: int, labels: builtins.list[str]) -> builtins.list[Label]:
        return await self._github.add_labels(self._owner, self._repo, number, labels)

    async def get_reviews(self, number: int) -> builtins.list[Review]:
        return await self._github.get_pr_reviews(self._owner, self._repo, number)

    async def get_comments(self, number: int) -> builtins.list[Comment]:
        return await self._github.get_pr_comments(self._owner, self._repo, number)


class GitHubRepositoryTools:
    """Container for repo-bound GitHub issue and pull-request tools."""

    def __init__(self, github: GitHubClient, owner: str, repo: str) -> None:
        self.issues = GitHubIssueTools(github, owner, repo)
        self.pull_requests = GitHubPullRequestTools(github, owner, repo)
