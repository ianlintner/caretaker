"""GitHub REST API client."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .models import (
    CheckRun,
    Comment,
    Issue,
    Label,
    PullRequest,
    Repository,
    Review,
    User,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {message}")


class GitHubClient:
    """Async GitHub REST API client."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise ValueError("GITHUB_TOKEN is required")
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path, **kwargs)
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

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _patch(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

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
        return data.get("state", "pending")

    # ── Issues ──────────────────────────────────────────────────

    async def list_issues(
        self, owner: str, repo: str, state: str = "open", labels: str | None = None
    ) -> list[Issue]:
        params: dict[str, Any] = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = labels
        data = await self._get(f"/repos/{owner}/{repo}/issues", params=params)
        # GitHub returns PRs in the issues endpoint — filter them out
        return [
            self._parse_issue(i) for i in (data or []) if "pull_request" not in i
        ]

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> Issue:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        data = await self._post(f"/repos/{owner}/{repo}/issues", json=payload)
        return self._parse_issue(data)

    async def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        **kwargs: Any,
    ) -> Issue:
        data = await self._patch(f"/repos/{owner}/{repo}/issues/{number}", json=kwargs)
        return self._parse_issue(data)

    async def add_issue_comment(
        self, owner: str, repo: str, number: int, body: str
    ) -> Comment:
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
        return [Label(name=l["name"], color=l.get("color", "")) for l in (data or [])]

    # ── Workflow dispatch ───────────────────────────────────────

    async def re_run_workflow(self, owner: str, repo: str, run_id: int) -> bool:
        result = await self._post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun")
        return result is None  # 204 = success

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
                Label(name=l["name"], color=l.get("color", ""))
                for l in data.get("labels", [])
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
                Label(name=l["name"], color=l.get("color", ""))
                for l in data.get("labels", [])
            ],
            assignees=[
                User(login=a["login"], id=a["id"]) for a in data.get("assignees", [])
            ],
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
