"""GitHub REST API client."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import httpx

from caretaker.tools.github import CopilotAgentAssignment
from caretaker.util.text import ensure_trailing_newline

if TYPE_CHECKING:
    from caretaker.tools.github import GitHubRepositoryTools

from .credentials import EnvCredentialsProvider, GitHubCredentialsProvider
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
from .rate_limit import (
    get_cooldown,
    record_rate_limit_response,
    record_response_headers,
)
from .scope_gap import (
    get_tracker as _scope_tracker,
)
from .scope_gap import (
    is_scope_gap_message,
    record_scope_gap_metric,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
COPILOT_ASSIGNEE_LOGIN = "copilot-swe-agent[bot]"
COPILOT_COMMENT_MARKERS = (
    "@copilot",
    "<!-- caretaker:task -->",
)


class GitHubAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"GitHub API error {status_code}: {message}")


class RateLimitError(GitHubAPIError):
    """Raised when GitHub refuses a request due to primary or secondary
    rate limiting, or when the process is short-circuiting because a
    prior rate-limit response is still in its cooldown window.

    Carries ``retry_after_seconds`` (may be ``None`` if the server
    didn't send a hint). Callers that can defer the work should catch
    this specifically; callers that must make the call are free to
    let it propagate.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(status_code, message)
        self.retry_after_seconds = retry_after_seconds


class GitHubClient:
    """Async GitHub REST API client."""

    def __init__(
        self,
        token: str | None = None,
        copilot_token: str | None = None,
        credentials_provider: GitHubCredentialsProvider | None = None,
        comment_cap_per_issue: int = 25,
    ) -> None:
        if credentials_provider is not None:
            self._creds: GitHubCredentialsProvider = credentials_provider
        else:
            # Backward-compat: wrap string tokens or fall back to env vars.
            self._creds = EnvCredentialsProvider(default_token=token, copilot_token=copilot_token)
        self._client = self._build_client()
        # In-process read cache: avoids redundant GET calls within a single run.
        # Keys are "path?param=value&..." strings; values are parsed JSON responses.
        self._read_cache: dict[str, Any] = {}
        # Defensive belt: refuse to post a *new* caretaker-marker comment to an
        # issue that already has this many caretaker-marker comments. Catches
        # future regressions of the duplicate-comment-storm pattern. Set to 0
        # to disable. Upserts and edits are unaffected.
        self._comment_cap_per_issue = max(0, comment_cap_per_issue)

    @staticmethod
    def _build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_default_token(self, *, installation_id: int | None = None) -> str:
        """Return the write-capable installation/env token used for API calls.

        Exposed for callers that must hand the token to subprocesses (e.g. the
        Foundry executor's ``git push`` path) without reaching into
        ``self._creds`` directly.
        """
        return await self._creds.default_token(installation_id=installation_id)

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
        # Short-circuit if a prior response told us to back off. Every
        # GitHubClient in the process shares this cooldown, so one agent
        # hitting a rate limit pauses the rest of the run instead of
        # burning more budget fire-and-forget.
        cooldown = get_cooldown()
        if cooldown.is_blocked():
            remaining = cooldown.seconds_remaining()
            snap = cooldown.snapshot()
            # Short-circuited call still counts as a ratelimit error
            # event for error-budget dashboards.
            try:
                from caretaker.observability.metrics import record_error

                record_error("ratelimit")
            except Exception:  # pragma: no cover - observability must never cascade
                pass
            raise RateLimitError(
                429,
                f"Short-circuit: GitHub rate-limit cooldown still active "
                f"({remaining:.0f}s remaining, reason={snap.get('reason')!r}).",
                retry_after_seconds=remaining,
            )

        # Time the call for http_client_* metrics. The start timestamp
        # is captured outside the try/except so network-level failures
        # still produce a latency sample with status_code=0.
        import time as _time

        _start = _time.perf_counter()
        status_code = 0
        try:
            resp = await client.request(method, path, **kwargs)
            status_code = resp.status_code
        except Exception:
            try:
                from caretaker.observability.metrics import record_error, record_http_client

                record_http_client(
                    peer_service="github",
                    method=method,
                    status_code=0,
                    duration=_time.perf_counter() - _start,
                )
                record_error("upstream")
            except Exception:  # pragma: no cover
                pass
            raise
        else:
            try:
                from caretaker.observability.metrics import record_http_client

                record_http_client(
                    peer_service="github",
                    method=method,
                    status_code=status_code,
                    duration=_time.perf_counter() - _start,
                )
            except Exception:  # pragma: no cover
                pass

        # Always sample rate-limit headers — lets us soft-throttle before
        # the bucket hits zero.
        record_response_headers(resp)

        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            until = record_rate_limit_response(resp, status_code=429)
            retry_after = max(0.0, until - time.time())
            raise RateLimitError(
                429,
                f"Rate limited. Retry after {retry_after:.0f}s.",
                retry_after_seconds=retry_after,
            )
        if resp.status_code == 403:
            # GitHub returns 403 (not 429) for installation/secondary rate limits.
            try:
                body = resp.json()
                message = body.get("message", "")
            except Exception:
                message = resp.text
            if "rate limit" in message.lower():
                until = record_rate_limit_response(resp, status_code=403)
                retry_after = max(0.0, until - time.time())
                raise RateLimitError(
                    403,
                    f"Rate limited. Retry after {retry_after:.0f}s.",
                    retry_after_seconds=retry_after,
                )
            # Token-scope gap: GitHub's "Resource not accessible by integration"
            # response means the workflow token is missing a required scope.
            # Aggregate to a single per-run issue instead of five silent warnings.
            if is_scope_gap_message(message):
                incident = _scope_tracker().record(method, path)
                record_scope_gap_metric(incident.scope_hint)
                logger.warning(
                    "GitHub 403 scope-gap: %s %s needs %s (count=%d in this run)",
                    method,
                    path,
                    incident.scope_hint,
                    incident.count,
                )
            raise GitHubAPIError(resp.status_code, resp.text)
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text)
        if resp.status_code == 204:
            return None
        return resp.json()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self._creds.default_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        return await self._request_with_client(
            self._client, method, path, headers=headers, **kwargs
        )

    async def _copilot_request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self._creds.copilot_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        return await self._request_with_client(
            self._client, method, path, headers=headers, **kwargs
        )

    async def _get(self, path: str, **kwargs: Any) -> Any:
        cache_key = self._make_cache_key(path, kwargs)
        if cache_key in self._read_cache:
            logger.debug("read-cache hit: %s", cache_key)
            return self._read_cache[cache_key]
        result = await self._request("GET", path, **kwargs)
        if result is not None:
            self._read_cache[cache_key] = result
        return result

    @staticmethod
    def _make_cache_key(path: str, kwargs: dict[str, Any]) -> str:
        """Build a deterministic cache key from a path and optional request kwargs.

        Only the ``params`` query-string values influence the key because all
        ``_get`` calls in this client share the same auth headers (set at
        build time) and never pass a request body.
        """
        params: dict[str, Any] | None = kwargs.get("params")
        if not params:
            return path
        return f"{path}?{urlencode(sorted(params.items()))}"

    def clear_read_cache(self) -> None:
        """Discard all cached GET responses.

        Call this after a write operation when the next read must reflect the
        mutation (e.g. after merging a PR or creating an issue).
        """
        self._read_cache.clear()

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _patch(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

    async def _copilot_post(self, path: str, **kwargs: Any) -> Any:
        return await self._copilot_request("POST", path, **kwargs)

    @staticmethod
    def _should_use_copilot_comment_client(body: str) -> bool:
        body_casefolded = body.casefold()
        return any(marker in body_casefolded for marker in COPILOT_COMMENT_MARKERS)

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

    async def get_issue(self, owner: str, repo: str, number: int) -> Issue | None:
        """Fetch a single issue by number. Returns ``None`` when missing."""
        data = await self._get(f"/repos/{owner}/{repo}/issues/{number}")
        if data is None:
            return None
        return self._parse_issue(data)

    async def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        """Return the list of files changed in a pull request.

        Each entry carries at minimum ``path``, ``additions``, ``deletions``,
        and ``status``. Used by dedupe logic to fingerprint PRs by their
        primary touched file.
        """
        data = await self._get(
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": 100},
        )
        if not data:
            return []
        return [
            {
                "path": f.get("filename", ""),
                "additions": int(f.get("additions", 0)),
                "deletions": int(f.get("deletions", 0)),
                "status": f.get("status", ""),
            }
            for f in data
        ]

    async def get_closing_issue_numbers(self, owner: str, repo: str, number: int) -> list[int]:
        """Return issue numbers this PR will close when merged.

        Uses the GraphQL ``closingIssuesReferences`` connection which reflects
        both body-text ``Fixes #N`` references and the "Development" sidebar
        links that Copilot-authored PRs rely on (body text is often absent).
        Returns an empty list if the query fails or no issues are linked.
        """
        query = (
            "query($owner:String!,$name:String!,$number:Int!){"
            " repository(owner:$owner,name:$name){"
            "  pullRequest(number:$number){"
            "   closingIssuesReferences(first:20){nodes{number}}"
            "  }"
            " }"
            "}"
        )
        try:
            result = await self._post(
                "/graphql",
                json={
                    "query": query,
                    "variables": {"owner": owner, "name": repo, "number": number},
                },
            )
        except Exception as e:
            logger.warning("GraphQL closingIssuesReferences for PR #%d failed: %s", number, e)
            return []
        if not result:
            return []
        try:
            nodes = result["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        except (KeyError, TypeError):
            return []
        return [int(n["number"]) for n in nodes if n and "number" in n]

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

    async def get_pull_diff(self, owner: str, repo: str, number: int) -> str:
        """Return the unified diff for a pull request as a string."""
        token = await self._creds.default_token()
        resp = await self._client.get(
            f"/repos/{owner}/{repo}/pulls/{number}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.diff",
            },
        )
        if resp.status_code == 404:
            return ""
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text)
        return resp.text

    async def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a pull request review.

        Args:
            event: One of ``APPROVE``, ``REQUEST_CHANGES``, ``COMMENT``.
            comments: Optional list of inline comments — each dict should
                carry ``path``, ``line``, ``body`` and optionally ``side``.
        """
        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = comments
        data = await self._post(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", json=payload)
        return data if data else {}

    async def request_reviewers(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Request specific reviewers for a pull request."""
        payload: dict[str, Any] = {"reviewers": reviewers}
        if team_reviewers:
            payload["team_reviewers"] = team_reviewers
        data = await self._post(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
            json=payload,
        )
        return data if data else {}

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

    async def create_check_run(
        self,
        owner: str,
        repo: str,
        name: str,
        head_sha: str,
        status: str = "in_progress",
        conclusion: str | None = None,
        output_title: str | None = None,
        output_summary: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a check run (status check) on a commit.

        Args:
            owner: Repository owner
            repo: Repository name
            name: Check run name (e.g. 'caretaker/pr-readiness')
            head_sha: The SHA of the commit to check
            status: 'queued', 'in_progress', 'completed'
            conclusion: 'success', 'failure', 'neutral', 'cancelled', 'skipped',
                       'timed_out', 'action_required', 'neutral', 'stale'
            output_title: Title for the check output
            output_summary: Summary text for the check output
            started_at: ISO 8601 timestamp when the check started
            completed_at: ISO 8601 timestamp when the check completed

        Returns:
            The created check run dict from the GitHub API
        """
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
        }
        if conclusion:
            payload["conclusion"] = conclusion
        if output_title or output_summary:
            payload["output"] = {
                "title": output_title or name,
                "summary": output_summary or "",
            }
        if started_at:
            payload["started_at"] = started_at
        if completed_at:
            payload["completed_at"] = completed_at

        data = await self._post(f"/repos/{owner}/{repo}/check-runs", json=payload)
        return data if data else {}

    async def update_check_run(
        self,
        owner: str,
        repo: str,
        check_run_id: int,
        status: str = "completed",
        conclusion: str | None = None,
        output_title: str | None = None,
        output_summary: str | None = None,
        completed_at: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Update an existing check run.

        Args:
            owner: Repository owner
            repo: Repository name
            check_run_id: The ID of the check run to update
            status: 'queued', 'in_progress', 'completed'
            conclusion: 'success', 'failure', 'neutral', 'cancelled', 'skipped',
                       'timed_out', 'action_required', 'neutral', 'stale'
            output_title: Title for the check output
            output_summary: Summary text for the check output
            completed_at: ISO 8601 timestamp when the check completed
            actions: Optional list of action buttons to add to the check

        Returns:
            The updated check run dict from the GitHub API
        """
        payload: dict[str, Any] = {
            "status": status,
        }
        if conclusion:
            payload["conclusion"] = conclusion
        if output_title or output_summary:
            payload["output"] = {
                "title": output_title or "",
                "summary": output_summary or "",
            }
        if completed_at:
            payload["completed_at"] = completed_at
        if actions:
            payload["actions"] = actions

        data = await self._patch(
            f"/repos/{owner}/{repo}/check-runs/{check_run_id}",
            json=payload,
        )
        return data if data else {}

    async def find_check_run(self, owner: str, repo: str, ref: str, name: str) -> CheckRun | None:
        """Find an existing check run by name on a given ref (branch/SHA).

        Returns the most recent check run with the matching name, or None if not found.
        """
        check_runs = await self.get_check_runs(owner, repo, ref)
        for cr in check_runs:
            if cr.name == name:
                return cr
        return None

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
        milestone: int | None = None,
    ) -> Issue:
        # Copilot assignment requires the dedicated issue-assignees flow with a user token.
        assign_copilot = any(is_copilot_login(assignee) for assignee in (assignees or []))
        real_assignees = [a for a in (assignees or []) if not is_copilot_login(a)]

        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if real_assignees:
            payload["assignees"] = real_assignees
        if milestone is not None:
            payload["milestone"] = milestone
        data = await self._post(f"/repos/{owner}/{repo}/issues", json=payload)
        issue = self._parse_issue(data)

        if assign_copilot:
            try:
                await self.assign_copilot_to_issue(
                    owner,
                    repo,
                    issue.number,
                    assignment=copilot_assignment,
                )
            except GitHubAPIError as exc:
                logger.warning(
                    "Copilot assignment failed for issue #%d in %s/%s (non-fatal): %s",
                    issue.number,
                    owner,
                    repo,
                    exc,
                )

        return issue

    async def assign_copilot_to_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        assignment: CopilotAgentAssignment | None = None,
    ) -> None:
        """Assign GitHub Copilot to an issue via the supported assignees endpoint."""
        agent_assignment = assignment or CopilotAgentAssignment(target_repo=f"{owner}/{repo}")
        payload: dict[str, Any] = {
            "assignees": [COPILOT_ASSIGNEE_LOGIN],
            "agent_assignment": agent_assignment.to_api_payload(),
        }
        result = await self._copilot_post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
            json=payload,
        )
        if result is None:
            raise GitHubAPIError(
                404,
                f"Unable to assign Copilot to issue #{issue_number} in {owner}/{repo}",
            )

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
            try:
                await self.assign_copilot_to_issue(
                    owner,
                    repo,
                    number,
                    assignment=copilot_assignment,
                )
            except GitHubAPIError as exc:
                logger.warning(
                    "Copilot assignment failed for issue #%d in %s/%s (non-fatal): %s",
                    number,
                    owner,
                    repo,
                    exc,
                )

        return issue

    async def create_milestone(
        self,
        owner: str,
        repo: str,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
    ) -> dict[str, Any]:
        """Create a repository milestone. Returns the raw milestone JSON."""
        payload: dict[str, Any] = {"title": title}
        if description is not None:
            payload["description"] = description
        if due_on is not None:
            payload["due_on"] = due_on
        data = await self._post(f"/repos/{owner}/{repo}/milestones", json=payload)
        return data or {}

    async def update_milestone(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        state: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update a milestone (e.g. ``state='closed'``)."""
        payload: dict[str, Any] = {}
        if state is not None:
            payload["state"] = state
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if not payload:
            data = await self._get(f"/repos/{owner}/{repo}/milestones/{number}")
        else:
            data = await self._patch(f"/repos/{owner}/{repo}/milestones/{number}", json=payload)
        return data or {}

    async def get_milestone_issues(
        self,
        owner: str,
        repo: str,
        milestone_number: int,
        state: str = "all",
    ) -> list[Issue]:
        """List issues associated with a milestone."""
        params: dict[str, Any] = {
            "milestone": milestone_number,
            "state": state,
            "per_page": 100,
        }
        data = await self._get(f"/repos/{owner}/{repo}/issues", params=params)
        return [self._parse_issue(i) for i in (data or []) if "pull_request" not in i]

    async def add_issue_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        *,
        use_copilot_token: bool | None = None,
    ) -> Comment:
        """Add an issue or PR comment.

        When ``use_copilot_token`` is left as ``None``, comments that summon
        ``@copilot`` (or carry a maintainer task marker) are routed through the
        PAT-backed client so GitHub attributes them to the configured write-capable
        identity instead of the default workflow bot.

        Defensive cap: if ``body`` carries a ``caretaker:`` marker AND the
        target issue already has at least ``comment_cap_per_issue`` such
        marker comments, the post is refused with a ``GitHubAPIError``
        (status 0). This is a belt-and-suspenders safeguard against future
        duplicate-comment-storm regressions; the normal path uses
        ``upsert_issue_comment`` which never trips the cap.
        """
        if self._comment_cap_per_issue > 0 and "caretaker:" in body:
            try:
                existing = await self.get_pr_comments(owner, repo, number)
            except Exception:
                existing = []  # if we can't read, don't block writes
            caretaker_count = sum(1 for c in existing if c.body and "caretaker:" in c.body)
            if caretaker_count >= self._comment_cap_per_issue:
                msg = (
                    f"Refusing to add caretaker comment to {owner}/{repo}#{number}: "
                    f"cap {self._comment_cap_per_issue} caretaker-marker comments "
                    "already present. Likely a duplicate-comment-storm bug."
                )
                logger.warning(msg)
                raise GitHubAPIError(0, msg)

        post = self._post
        if use_copilot_token is True or (
            use_copilot_token is None and self._should_use_copilot_comment_client(body)
        ):
            post = self._copilot_post

        data = await post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return self._parse_comment(data)

    async def edit_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        body: str,
    ) -> Comment:
        """Edit an existing issue/PR comment by id via PATCH."""
        data = await self._patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return self._parse_comment(data)

    async def delete_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
    ) -> None:
        """Delete an existing issue/PR comment by id via DELETE."""
        await self._request(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
        )

    async def upsert_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_markers: tuple[str, ...] = (),
        min_seconds_between_updates: int = 0,
    ) -> Comment:
        """Post or edit a single issue/PR comment identified by ``marker``.

        ``marker`` MUST be a unique HTML-comment substring (e.g.
        ``"<!-- caretaker:orchestrator-state -->"``) and MUST appear in
        ``body``. The first comment whose body contains ``marker`` (or any
        ``legacy_markers`` entry, when supplied) is edited in place; if none
        exists, a new comment is posted. Idempotent: a no-op if an existing
        matching comment already has the same body.

        ``legacy_markers`` lets callers migrate from older marker spellings
        without leaving stale duplicates behind.

        ``min_seconds_between_updates`` enforces a per-marker cooldown: when
        > 0, an existing matching comment whose ``updated_at`` (or
        ``created_at`` if never edited) is more recent than that many seconds
        ago is left untouched (cooldown active). New posts and edits beyond
        the cooldown are unaffected. This guards against rapid retrigger
        loops independent of the per-issue count cap on
        :meth:`add_issue_comment`.
        """
        if marker not in body:
            raise ValueError(f"upsert body missing marker {marker!r}")

        all_markers = (marker, *legacy_markers)
        comments = await self.get_pr_comments(owner, repo, issue_number)

        existing: Comment | None = None
        for c in comments:
            if not (c.body or ""):
                continue
            if any(m in c.body for m in all_markers) and (existing is None or c.id > existing.id):
                existing = c

        if existing is None:
            return await self.add_issue_comment(owner, repo, issue_number, body)

        if (existing.body or "").strip() == body.strip():
            return existing

        if min_seconds_between_updates > 0:
            from datetime import UTC
            from datetime import datetime as _dt

            ref = existing.updated_at or existing.created_at
            if ref is not None:
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=UTC)
                age = (_dt.now(UTC) - ref).total_seconds()
                if age < min_seconds_between_updates:
                    logger.info(
                        "upsert cooldown active on %s/%s#%d marker=%s "
                        "(age=%.0fs < cooldown=%ds) — skipping update",
                        owner,
                        repo,
                        issue_number,
                        marker,
                        age,
                        min_seconds_between_updates,
                    )
                    return existing

        return await self.edit_issue_comment(owner, repo, existing.id, body)

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

    async def approve_workflow_run(self, owner: str, repo: str, run_id: int) -> bool:
        """Approve a workflow run for a fork pull request."""
        token = await self._creds.default_token()
        response = await self._client.post(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 404:
            return False
        if response.status_code == 204:
            return True
        if response.is_success:
            if response.content:
                data = response.json()
                return bool(isinstance(data, dict) and data.get("id"))
            return True
        message = response.text
        if not message:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                message = str(payload.get("message", payload))
            elif payload is not None:
                message = str(payload)
            else:
                message = "Request failed"
        raise GitHubAPIError(response.status_code, message)

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
        """Create or update a file via the contents API. *content* is raw UTF-8 text.

        The content is passed through :func:`ensure_trailing_newline` before
        being base64-encoded so the resulting blob ends with a single ``\\n``.
        This keeps consumer-side pre-commit ``end-of-file-fixer`` hooks happy
        on files caretaker writes (CHANGELOG.md, workflow YAMLs, etc.) — see
        PR python_dsa#42 / kubernetes-apply-vscode#17 for the failure mode
        this prevents.
        """
        import base64

        normalised = ensure_trailing_newline(content)
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(normalised.encode()).decode(),
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
        head = data.get("head") or {}
        base = data.get("base") or {}
        head_repo = (head.get("repo") or {}).get("full_name", "") or ""
        base_repo = (base.get("repo") or {}).get("full_name", "") or ""
        return PullRequest(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            state=data["state"],
            user=User(login=data["user"]["login"], id=data["user"]["id"]),
            head_ref=head.get("ref", ""),
            head_sha=head.get("sha", ""),
            base_ref=base.get("ref", ""),
            head_repo_full_name=head_repo,
            base_repo_full_name=base_repo,
            mergeable=data.get("mergeable"),
            merged=data.get("merged", False),
            draft=data.get("draft", False),
            labels=[
                Label(name=lbl["name"], color=lbl.get("color", ""))
                for lbl in data.get("labels", [])
            ],
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            merged_at=data.get("merged_at"),
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
