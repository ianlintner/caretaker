"""GitHub REST API client."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, cast

import httpx

from caretaker.tools.github import CopilotAgentAssignment

from .models import (
    CheckRun,
    Comment,
    Issue,
    Label,
    PullRequest,
    Repository,
    Review,
    User,
    is_copilot_login,
)

if TYPE_CHECKING:
    from caretaker.tools.github import GitHubRepositoryTools

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
COPILOT_ASSIGNEE_LOGIN = "copilot-swe-agent[bot]"


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {message}")


class GitHubClient:
    """Async GitHub REST API client."""

    def __init__(
        self,
        token: str | None = None,
        copilot_token: str | None = None,
    ) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("COPILOT_PAT", "")
        if not self._token:
            raise ValueError("GITHUB_TOKEN or COPILOT_PAT is required")
        self._copilot_token = copilot_token or os.environ.get("COPILOT_PAT") or self._token
        self._client = self._build_client(self._token)
        self._copilot_client = (
            self._client
            if self._copilot_token == self._token
            else self._build_client(self._copilot_token)
        )

    @staticmethod
    def _build_client(token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._copilot_client is not self._client:
            await self._copilot_client.aclose()
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _request_with_client(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        resp = await client.request(method, path, **kwargs)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "60")
            raise GitHubAPIError(429, f"Rate limited. Retry after {retry_after}s")
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text)
        if resp.status_code == 204:
            return None
        return resp.json()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._request_with_client(self._client, method, path, **kwargs)

    async def _copilot_request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._request_with_client(self._copilot_client, method, path, **kwargs)

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _patch(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

    async def _copilot_post(self, path: str, **kwargs: Any) -> Any:
        return await self._copilot_request("POST", path, **kwargs)

    def for_repo(self, owner: str, repo: str) -> GitHubRepositoryTools:
        """Return a repo-bound toolset for issue and pull-request operations."""
        from caretaker.tools.github import GitHubRepositoryTools

        return GitHubRepositoryTools(self, owner, repo)

    # ── Repository ──────────────────────────────────────────────

    async def get_repo(self, owner: str, repo: str) -> Repository:
        data = await self._get(f"/repos/{owner}/{repo}")
        return Repository(
            owner=data["owner"]["login"],
            name=data["name"],
            full_name=data["full_name"],
            default_branch=data.get("default_branch", "main"),
            private=data.get("private", False),
        )

    # ── Pull Requests ───────────────────────────────────────────

    async def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[PullRequest]:
        data = await self._get(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": 100},
        )
        return [self._parse_pr(pr) for pr in (data or [])]

    async def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest | None:
        data = await self._get(f"/repos/{owner}/{repo}/pulls/{number}")
        if data is None:
            return None
        return self._parse_pr(data)

    async def merge_pull_request(
        self, owner: str, repo: str, number: int, method: str = "squash"
    ) -> bool:
        result = await self._put(
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"merge_method": method},
        )
        return result is not None and result.get("merged", False)

    async def get_pr_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        data = await self._get(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        return [
            Review(
                id=r["id"],
                user=User(login=r["user"]["login"], id=r["user"]["id"]),
                state=r["state"],
                body=r.get("body", ""),
                submitted_at=r.get("submitted_at"),
            )
            for r in (data or [])
        ]

    async def get_pr_comments(self, owner: str, repo: str, number: int) -> list[Comment]:
        data = await self._get(f"/repos/{owner}/{repo}/issues/{number}/comments")
        return [self._parse_comment(c) for c in (data or [])]

    # ── Check Runs (CI) ────────────────────────────────────────

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[CheckRun]:
        data = await self._get(f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
        if not data:
            return []
        return [
            CheckRun(
                id=cr["id"],
                name=cr["name"],
                status=cr["status"],
                conclusion=cr.get("conclusion"),
                started_at=cr.get("started_at"),
                completed_at=cr.get("completed_at"),
                html_url=cr.get("html_url", ""),
                output_title=cr.get("output", {}).get("title"),
                output_summary=cr.get("output", {}).get("summary"),
            )
            for cr in data.get("check_runs", [])
        ]

    async def get_combined_status(self, owner: str, repo: str, ref: str) -> str:
        """Get combined commit status: 'success', 'failure', 'pending'."""
        data = await self._get(f"/repos/{owner}/{repo}/commits/{ref}/status")
        if not data:
            return "pending"
        return str(data.get("state", "pending"))

    # ── Issues ──────────────────────────────────────────────────

    async def list_issues(
        self, owner: str, repo: str, state: str = "open", labels: str | None = None
    ) -> list[Issue]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = labels
        data = await self._get(f"/repos/{owner}/{repo}/issues", params=params)
        # GitHub returns PRs in the issues endpoint — filter them out
        return [self._parse_issue(i) for i in (data or []) if "pull_request" not in i]

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        copilot_assignment: CopilotAgentAssignment | None = None,
    ) -> Issue:
        # Copilot assignment requires the dedicated issue-assignees flow with a user token.
        assign_copilot = any(is_copilot_login(assignee) for assignee in (assignees or []))
        real_assignees = [a for a in (assignees or []) if not is_copilot_login(a)]

        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if real_assignees:
            payload["assignees"] = real_assignees
        data = await self._post(f"/repos/{owner}/{repo}/issues", json=payload)
        issue = self._parse_issue(data)

        if assign_copilot:
            await self.assign_copilot_to_issue(
                owner,
                repo,
                issue.number,
                assignment=copilot_assignment,
            )

        return issue

    async def assign_copilot_to_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        assignment: CopilotAgentAssignment | None = None,
    ) -> None:
        """Assign GitHub Copilot to an issue via the supported assignees endpoint.

        Logs a warning and returns without raising if the API rejects the request
        (e.g. 403 when a COPILOT_PAT with the required scope is not configured).
        """
        agent_assignment = assignment or CopilotAgentAssignment(target_repo=f"{owner}/{repo}")
        payload: dict[str, Any] = {
            "assignees": [COPILOT_ASSIGNEE_LOGIN],
            "agent_assignment": agent_assignment.to_api_payload(),
        }
        try:
            await self._copilot_post(
                f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
                json=payload,
            )
        except GitHubAPIError as exc:
            if exc.status_code in (403, 422):
                logger.warning(
                    "Unable to assign Copilot to issue #%d in %s/%s (HTTP %d). "
                    "Ensure COPILOT_PAT is configured with the required scope.",
                    issue_number,
                    owner,
                    repo,
                    exc.status_code,
                )
                return
            raise

    async def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        **kwargs: Any,
    ) -> Issue:
        # Copilot assignment requires the dedicated issue-assignees flow with a user token.
        copilot_assignment = kwargs.pop("copilot_assignment", None)
        assignees: list[str] | None = kwargs.get("assignees")
        assign_copilot = assignees is not None and any(
            is_copilot_login(assignee) for assignee in assignees
        )
        if assign_copilot and assignees is not None:
            kwargs["assignees"] = [a for a in assignees if not is_copilot_login(a)]

        if kwargs:
            data = await self._patch(f"/repos/{owner}/{repo}/issues/{number}", json=kwargs)
        else:
            data = await self._get(f"/repos/{owner}/{repo}/issues/{number}")
        issue = self._parse_issue(data)

        if assign_copilot:
            await self.assign_copilot_to_issue(
                owner,
                repo,
                number,
                assignment=copilot_assignment,
            )

        return issue

    async def add_issue_comment(self, owner: str, repo: str, number: int, body: str) -> Comment:
        data = await self._post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return self._parse_comment(data)

    async def add_labels(
        self, owner: str, repo: str, number: int, labels: list[str]
    ) -> list[Label]:
        data = await self._post(
            f"/repos/{owner}/{repo}/issues/{number}/labels",
            json={"labels": labels},
        )
        return [Label(name=lbl["name"], color=lbl.get("color", "")) for lbl in (data or [])]

    # ── Workflow dispatch ───────────────────────────────────────

    async def re_run_workflow(self, owner: str, repo: str, run_id: int) -> bool:
        result = await self._post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun")
        return result is None  # 204 = success

    # ── Labels ──────────────────────────────────────────────────

    async def ensure_label(
        self, owner: str, repo: str, name: str, color: str, description: str = ""
    ) -> None:
        """Create the label if it does not already exist."""
        existing = await self._get(f"/repos/{owner}/{repo}/labels/{name}")
        if existing is not None:
            return
        try:
            await self._post(
                f"/repos/{owner}/{repo}/labels",
                json={"name": name, "color": color, "description": description},
            )
        except GitHubAPIError as e:
            if e.status_code == 422:
                pass  # Already exists (race condition)
            else:
                raise

    # ── Security alerts ─────────────────────────────────────────

    async def list_dependabot_alerts(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{owner}/{repo}/dependabot/alerts",
            params={"state": state, "per_page": 100},
        )
        return data or []

    async def dismiss_dependabot_alert(
        self,
        owner: str,
        repo: str,
        alert_number: int,
        reason: str,
        comment: str = "",
    ) -> None:
        await self._patch(
            f"/repos/{owner}/{repo}/dependabot/alerts/{alert_number}",
            json={
                "state": "dismissed",
                "dismissed_reason": reason,
                "dismissed_comment": comment,
            },
        )

    async def list_code_scanning_alerts(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{owner}/{repo}/code-scanning/alerts",
            params={"state": state, "per_page": 100},
        )
        return data or []

    async def dismiss_code_scanning_alert(
        self,
        owner: str,
        repo: str,
        alert_number: int,
        reason: str,
        comment: str = "",
    ) -> None:
        await self._patch(
            f"/repos/{owner}/{repo}/code-scanning/alerts/{alert_number}",
            json={
                "state": "dismissed",
                "dismissed_reason": reason,
                "dismissed_comment": comment,
            },
        )

    async def list_secret_scanning_alerts(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/repos/{owner}/{repo}/secret-scanning/alerts",
            params={"state": state, "per_page": 100},
        )
        return data or []

    # ── Contents / branches / PRs ────────────────────────────────

    async def get_file_contents(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> dict[str, Any] | None:
        """Return the raw GitHub contents API response dict, or None if missing."""
        params = {}
        if ref:
            params["ref"] = ref
        data = await self._get(f"/repos/{owner}/{repo}/contents/{path}", params=params)
        return cast("dict[str, Any] | None", data)

    async def get_default_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        """Return the latest commit SHA of *branch*."""
        data = await self._get(f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        if not data:
            raise GitHubAPIError(404, f"Branch {branch!r} not found")
        return str(data["object"]["sha"])

    async def create_branch(self, owner: str, repo: str, name: str, sha: str) -> None:
        """Create a new branch pointing at *sha*."""
        await self._post(
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{name}", "sha": sha},
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        message: str,
        content: str,
        branch: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file via the contents API. *content* is raw UTF-8 text."""
        import base64

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        data = await self._put(f"/repos/{owner}/{repo}/contents/{path}", json=payload)
        return cast("dict[str, Any]", data) if data is not None else {}

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> dict[str, Any]:
        data = await self._post(
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        pr_number = data["number"]
        if labels:
            await self._post(
                f"/repos/{owner}/{repo}/issues/{pr_number}/labels",
                json={"labels": labels},
            )
        if assignees:
            await self._post(
                f"/repos/{owner}/{repo}/issues/{pr_number}/assignees",
                json={"assignees": assignees},
            )
        return cast("dict[str, Any]", data)

    async def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        await self._request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")

    # ── Parsing helpers ─────────────────────────────────────────

    @staticmethod
    def _parse_pr(data: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            state=data["state"],
            user=User(login=data["user"]["login"], id=data["user"]["id"]),
            head_ref=data.get("head", {}).get("ref", ""),
            base_ref=data.get("base", {}).get("ref", ""),
            mergeable=data.get("mergeable"),
            merged=data.get("merged", False),
            draft=data.get("draft", False),
            labels=[
                Label(name=lbl["name"], color=lbl.get("color", ""))
                for lbl in data.get("labels", [])
            ],
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            html_url=data.get("html_url", ""),
        )

    @staticmethod
    def _parse_issue(data: dict[str, Any]) -> Issue:
        return Issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            state=data["state"],
            user=User(login=data["user"]["login"], id=data["user"]["id"]),
            labels=[
                Label(name=lbl["name"], color=lbl.get("color", ""))
                for lbl in data.get("labels", [])
            ],
            assignees=[User(login=a["login"], id=a["id"]) for a in data.get("assignees", [])],
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            html_url=data.get("html_url", ""),
        )

    @staticmethod
    def _parse_comment(data: dict[str, Any]) -> Comment:
        return Comment(
            id=data["id"],
            user=User(login=data["user"]["login"], id=data["user"]["id"]),
            body=data.get("body") or "",
            created_at=data["created_at"],
            updated_at=data.get("updated_at"),
        )
