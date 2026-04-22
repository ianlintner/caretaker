"""End-to-end Foundry coding executor.

Accepts a :class:`CodingTask`, opens a git worktree over the PR's head SHA,
runs the LLM tool-loop, optionally runs an allowlisted lint/format command,
gates the diff through the size classifier, commits, pushes with
``--force-with-lease``, and posts a result comment compatible with the
existing Copilot state-machine markers.

On any non-success path the executor returns a result asking the caller to
fall back to Copilot, with a short reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from caretaker.evolution.executor_routing import (
    ExecutorRoute,
    ExecutorRouteContext,
    ExecutorRouteFile,
    executor_routes_agree,
    route_executor_llm,
    route_from_foundry_legacy,
)
from caretaker.evolution.shadow import shadow_decision
from caretaker.foundry.prompts import build_prompt
from caretaker.foundry.size_classifier import (
    ClassifierResult,
    Decision,
    post_flight,
    pre_flight,
)
from caretaker.foundry.tool_loop import ToolLoopError, run_tool_loop
from caretaker.foundry.tools import ToolContext, build_tool_registry
from caretaker.foundry.workspace import Workspace, WorkspaceError
from caretaker.llm.copilot import (
    RESULT_CLOSE,
    RESULT_OPEN,
    ResultStatus,
    TaskType,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any

    from caretaker.config import FoundryExecutorConfig
    from caretaker.evolution.insight_store import Skill
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest
    from caretaker.llm.claude import ClaudeClient
    from caretaker.llm.provider import LLMProvider

type TokenSupplier = Callable[[], Awaitable[str]]

logger = logging.getLogger(__name__)


@shadow_decision("executor_routing", compare=executor_routes_agree)
async def _decide_executor_route(
    *, legacy: Any, candidate: Any, context: Any = None
) -> ExecutorRoute:
    """Shadow-mode decision point wrapping the legacy + LLM routing paths.

    Placeholder body — the decorator short-circuits to ``legacy`` in
    ``off`` / ``shadow`` modes and to ``candidate`` in ``enforce`` mode.
    Shares the ``executor_routing`` name with the pr-reviewer call site
    so shadow data aggregates across both executors.
    """
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


class ExecutorOutcome(StrEnum):
    """High-level outcome of a :class:`FoundryExecutor.run` call."""

    COMPLETED = "COMPLETED"  # pushed + result comment posted
    ESCALATED = "ESCALATED"  # caller should fall back to Copilot
    FAILED = "FAILED"  # unrecoverable error


@dataclass
class CodingTask:
    """A coding task passed to the executor.

    Mirrors the fields of ``CopilotTask`` so the same data can route to either
    backend. The extra ``preferred_command`` carries an allowlisted command to
    run post-edit for verification (e.g. ``ruff check .``).
    """

    task_type: TaskType
    job_name: str
    error_output: str
    instructions: str
    context: str = ""
    preferred_command: tuple[str, list[str]] | None = None
    skills: list[Skill] = field(default_factory=list)


@dataclass
class ExecutorResult:
    """Return value of :meth:`FoundryExecutor.run`."""

    outcome: ExecutorOutcome
    reason: str = ""
    commit_sha: str | None = None
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    comment_id: int | None = None


def _build_result_comment(
    *,
    status: ResultStatus,
    commit_sha: str | None,
    files_changed: int,
    insertions: int,
    deletions: int,
    iterations: int,
    summary_text: str,
    blocker: str = "",
) -> str:
    """Format a result comment matching the existing ``caretaker:result`` schema.

    ``CopilotResult.parse`` at ``caretaker/llm/copilot.py`` keys off
    ``RESULT:/CHANGES:/TESTS:/COMMIT:/BLOCKED:`` lines so the emit format
    must match exactly for the PR state machine to work unchanged.
    """
    lines = [
        "caretaker-foundry result",
        "",
        RESULT_OPEN,
        f"RESULT: {status.value}",
        f"CHANGES: {files_changed} file(s), +{insertions}/-{deletions}",
        "TESTS: executor ran configured lint/format command",
    ]
    if commit_sha:
        lines.append(f"COMMIT: {commit_sha}")
    if blocker:
        lines.append(f"BLOCKED: {blocker}")
    lines.extend([RESULT_CLOSE, "", f"_foundry: {iterations} iteration(s)_"])
    if summary_text.strip():
        lines.extend(["", summary_text.strip()])
    return "\n".join(lines)


class FoundryExecutor:
    """Coordinates the workspace, tool loop, and result reporting."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: FoundryExecutorConfig,
        source_repo_path: Path | None = None,
        token_supplier: TokenSupplier | None = None,
        claude: ClaudeClient | None = None,
    ) -> None:
        self._provider = provider
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config
        self._source_repo_path = (
            Path(source_repo_path)
            if source_repo_path is not None
            else Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd()))
        )
        self._token_supplier = token_supplier
        # Optional ``ClaudeClient`` used by the executor_routing shadow
        # candidate. ``None`` disables the LLM path (shadow mode will
        # record candidate_error, enforce mode will fall through to
        # legacy), matching the default-safe rollout pattern used by
        # every other Phase 2 migration.
        self._claude = claude
        # Per-branch asyncio lock so two concurrent tasks on the same PR
        # branch don't clobber each other's worktree.
        self._branch_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, branch: str) -> asyncio.Lock:
        lock = self._branch_locks.get(branch)
        if lock is None:
            lock = asyncio.Lock()
            self._branch_locks[branch] = lock
        return lock

    async def _route_via_shadow(
        self,
        *,
        task: CodingTask,
        pr: PullRequest,
        classifier_result: ClassifierResult,
    ) -> ExecutorRoute | None:
        """Run the ``executor_routing`` shadow gate for the Foundry site.

        Returns the legacy-adapted :class:`ExecutorRoute` in
        ``off`` / ``shadow`` modes; returns the LLM verdict when
        ``enforce`` mode succeeds. Defensive: swallows every exception —
        the classifier verdict is still authoritative on the control
        path inside :meth:`run`.
        """

        async def _legacy_path() -> ExecutorRoute:
            return route_from_foundry_legacy(classifier_result)

        async def _candidate_path() -> ExecutorRoute | None:
            if self._claude is None or not self._claude.available:
                return None
            context = ExecutorRouteContext(
                task_type=task.task_type.value,
                files=[ExecutorRouteFile(path=pr.head_ref or f"pr-{pr.number}")],
                labels=list(getattr(pr, "labels", []) or []),
                repo_slug=f"{self._owner}/{self._repo}",
                candidate_paths=["foundry", "copilot"],
                title=task.job_name,
                body=task.instructions,
            )
            return await route_executor_llm(context, claude=self._claude)

        try:
            return await _decide_executor_route(
                legacy=_legacy_path,
                candidate=_candidate_path,
                context={
                    "pr_number": pr.number,
                    "repo_slug": f"{self._owner}/{self._repo}",
                    "site": "foundry",
                    "task_type": task.task_type.value,
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the executor
            logger.warning(
                "foundry: executor_routing shadow-decision failed for PR #%d (%s: %s)",
                pr.number,
                type(exc).__name__,
                exc,
            )
            return None

    async def run(self, task: CodingTask, pr: PullRequest) -> ExecutorResult:
        """Execute ``task`` end-to-end. Never raises — always returns an
        ``ExecutorResult``; the caller falls back to Copilot on non-COMPLETED.
        """
        # ── Pre-flight ──
        # When the PR model carries explicit repo identity (populated by
        # ``_parse_pr``) use it for the fork check. Fall back to the local
        # owner/repo when the PR was constructed by tests/fixtures without
        # the repo fields — that matches non-fork production behavior.
        head_repo = pr.head_repo_full_name or f"{self._owner}/{self._repo}"
        base_repo = pr.base_repo_full_name or f"{self._owner}/{self._repo}"

        pre = pre_flight(
            task_type=task.task_type.value,
            allowed_task_types=self._config.allowed_task_types,
            head_repo_full_name=head_repo,
            base_repo_full_name=base_repo,
            route_same_repo_only=self._config.route_same_repo_only,
            error_output=task.error_output,
        )

        # Shadow-mode wrapper around the classifier verdict. Shares the
        # ``executor_routing`` decoration name with the pr-reviewer call
        # site so disagreement data aggregates across both executors.
        # The control-flow remains driven by the legacy ``pre.decision``
        # field — this is purely observability until enforce mode flips.
        await self._route_via_shadow(task=task, pr=pr, classifier_result=pre)

        if pre.decision != Decision.ROUTE_FOUNDRY:
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=f"pre_flight: {pre.reason}",
            )

        branch = pr.head_ref or f"pr-{pr.number}"
        lock = self._lock_for(branch)

        try:
            async with lock, asyncio.timeout(self._config.workspace_timeout_seconds):
                return await self._run_locked(task, pr, branch)
        except TimeoutError:
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=f"workspace timeout ({self._config.workspace_timeout_seconds}s)",
            )
        except Exception as exc:
            logger.exception("FoundryExecutor run failed: %s", exc)
            return ExecutorResult(
                outcome=ExecutorOutcome.FAILED,
                reason=f"unhandled error: {exc}",
            )

    async def _run_locked(self, task: CodingTask, pr: PullRequest, branch: str) -> ExecutorResult:
        try:
            async with Workspace(
                source_repo=self._source_repo_path,
                head_sha=pr.head_sha,
            ) as workspace:
                tool_ctx = ToolContext(
                    workspace_root=workspace.path,
                    write_denylist=list(self._config.write_denylist),
                    allowed_commands=list(self._config.allowed_commands),
                )
                tools = build_tool_registry()
                prompt = build_prompt(
                    task.task_type,
                    job_name=task.job_name,
                    error_output=task.error_output,
                    instructions=task.instructions,
                    context=task.context,
                    skills=task.skills,
                    write_denylist=self._config.write_denylist,
                    allowed_commands=self._config.allowed_commands,
                )

                try:
                    loop_result = await run_tool_loop(
                        provider=self._provider,
                        system_prompt=prompt.system,
                        user_prompt=prompt.user,
                        tools=tools,
                        tool_ctx=tool_ctx,
                        model=self._config.model,
                        max_iterations=self._config.max_iterations,
                        token_budget=self._config.max_tokens_per_task,
                    )
                except ToolLoopError as exc:
                    return ExecutorResult(
                        outcome=ExecutorOutcome.ESCALATED,
                        reason=f"tool_loop: {exc}",
                    )

                # Optionally run a verification command (lint/format) after
                # the model finishes. Only uses allowlisted commands.
                if task.preferred_command is not None:
                    cmd, args = task.preferred_command
                    if cmd in self._config.allowed_commands:
                        await tools["run_command"].handler(tool_ctx, cmd, args)

                # Post-flight sizing gate
                diff_stats = await workspace.diff_stat()
                post = post_flight(
                    files_changed=diff_stats["files_changed"],
                    insertions=diff_stats["insertions"],
                    deletions=diff_stats["deletions"],
                    max_files_touched=self._config.max_files_touched,
                    max_diff_lines=self._config.max_diff_lines,
                )
                if post.decision != Decision.ROUTE_FOUNDRY:
                    return ExecutorResult(
                        outcome=ExecutorOutcome.ESCALATED,
                        reason=f"post_flight: {post.reason}",
                        files_changed=diff_stats["files_changed"],
                        insertions=diff_stats["insertions"],
                        deletions=diff_stats["deletions"],
                        iterations=loop_result.iterations,
                        input_tokens=loop_result.input_tokens,
                        output_tokens=loop_result.output_tokens,
                        cost_usd=loop_result.cost_usd,
                    )

                commit_message = self._commit_message_for(task)
                commit = await workspace.commit_all(commit_message)
                if commit.sha is None:
                    return ExecutorResult(
                        outcome=ExecutorOutcome.ESCALATED,
                        reason="no changes produced by tool loop",
                        iterations=loop_result.iterations,
                        input_tokens=loop_result.input_tokens,
                        output_tokens=loop_result.output_tokens,
                        cost_usd=loop_result.cost_usd,
                    )

                try:
                    remote_url = await self._resolve_remote_url()
                    await workspace.push(remote_url=remote_url, branch=branch)
                except WorkspaceError as exc:
                    return ExecutorResult(
                        outcome=ExecutorOutcome.ESCALATED,
                        reason=f"push failed: {exc}",
                        files_changed=commit.files_changed,
                        insertions=commit.insertions,
                        deletions=commit.deletions,
                        iterations=loop_result.iterations,
                        input_tokens=loop_result.input_tokens,
                        output_tokens=loop_result.output_tokens,
                        cost_usd=loop_result.cost_usd,
                    )

                comment_body = _build_result_comment(
                    status=ResultStatus.FIXED,
                    commit_sha=commit.sha,
                    files_changed=commit.files_changed,
                    insertions=commit.insertions,
                    deletions=commit.deletions,
                    iterations=loop_result.iterations,
                    summary_text=loop_result.final_text,
                )
                try:
                    comment = await self._github.add_issue_comment(
                        self._owner,
                        self._repo,
                        pr.number,
                        comment_body,
                        use_copilot_token=False,
                    )
                    comment_id = comment.id
                except Exception as exc:
                    logger.warning(
                        "Failed to post Foundry result comment on PR #%d: %s",
                        pr.number,
                        exc,
                    )
                    comment_id = None

                return ExecutorResult(
                    outcome=ExecutorOutcome.COMPLETED,
                    reason="pushed",
                    commit_sha=commit.sha,
                    files_changed=commit.files_changed,
                    insertions=commit.insertions,
                    deletions=commit.deletions,
                    iterations=loop_result.iterations,
                    input_tokens=loop_result.input_tokens,
                    output_tokens=loop_result.output_tokens,
                    cost_usd=loop_result.cost_usd,
                    comment_id=comment_id,
                )
        except WorkspaceError as exc:
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=f"workspace: {exc}",
            )

    def _commit_message_for(self, task: CodingTask) -> str:
        """Conventional-commit style message keyed off task type."""
        prefix = {
            TaskType.LINT_FAILURE: "style",
            TaskType.REVIEW_COMMENT: "refactor",
            TaskType.UPGRADE: "chore",
        }.get(task.task_type, "chore")
        scope = task.job_name.replace(" ", "-").replace("/", "-")[:30] or "foundry"
        return f"{prefix}({scope}): caretaker-foundry automated fix"

    async def _resolve_remote_url(self) -> str:
        """Build a push URL that includes a short-lived write token.

        Uses ``token_supplier`` when available; otherwise falls back to
        reading ``GITHUB_TOKEN`` from the environment (which is how the
        workflow runner has historically exposed the token).
        """
        token = ""
        if self._token_supplier is not None:
            token = await self._token_supplier()
        if not token:
            token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise WorkspaceError(
                "no push token available (set GITHUB_TOKEN or wire a token_supplier)"
            )
        return f"https://x-access-token:{token}@github.com/{self._owner}/{self._repo}.git"
