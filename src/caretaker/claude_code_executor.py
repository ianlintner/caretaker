"""Claude Code hand-off executor.

Caretaker's own tool-loop lives in :mod:`caretaker.foundry.executor`. The
*Claude Code* executor is a thin hand-off: it tags the host PR / issue
with a configurable trigger label and posts a structured mention comment
that the upstream `anthropics/claude-code-action`_ workflow picks up.

Design choices:

* **No inline execution.** Caretaker does not spawn Claude Code itself.
  The consumer repo is expected to have the upstream action installed;
  caretaker just steers tasks at it. This keeps caretaker backend-agnostic
  and avoids a second model-provider credential to manage.
* **Async hand-off.** The upstream action runs in its own workflow and
  posts its own commit + result comment. Caretaker's PR state machine
  already tracks those via the ``<!-- caretaker:result -->`` markers, so
  the hand-off looks identical to any other async executor.
* **COMPLETED on successful hand-off.** We treat the dispatch itself as
  the unit of work; failures to apply the label / post the comment
  escalate back to Copilot the same way a Foundry failure does.
* **Attempt cap.** If caretaker re-routes the same task to Claude Code
  more than ``config.max_attempts`` times without an upstream resolution,
  we stop re-triggering and escalate. Prevents the trigger-label →
  no-op → re-trigger loop if the upstream action is unavailable.

.. _anthropics/claude-code-action: https://github.com/anthropics/claude-code-action
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from caretaker.foundry.executor import (
    CodingTask,
    ExecutorOutcome,
    ExecutorResult,
)

if TYPE_CHECKING:
    from caretaker.config import ClaudeCodeExecutorConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


CLAUDE_CODE_HANDOFF_MARKER = "<!-- caretaker:claude-code-handoff -->"


def _build_handoff_comment(
    *, mention: str, task: CodingTask, attempt: int, max_attempts: int
) -> str:
    """Format the structured hand-off comment the upstream action consumes."""
    lines = [
        CLAUDE_CODE_HANDOFF_MARKER,
        f"{mention} caretaker is handing this task off to claude-code.",
        "",
        f"**task_type**: `{task.task_type.value}`",
        f"**job**: `{task.job_name}`",
        f"**attempt**: {attempt}/{max_attempts}",
        "",
        "**Instructions:**",
        task.instructions or "(none)",
    ]
    if task.error_output:
        snippet = task.error_output.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "\n…(truncated)"
        lines.extend(["", "**Error output:**", "```", snippet, "```"])
    if task.context:
        lines.extend(["", "**Additional context:**", task.context.strip()])
    lines.extend(
        [
            "",
            "_Applied by caretaker's ClaudeCodeExecutor. The upstream "
            "`anthropics/claude-code-action` workflow will produce the "
            "fix as its own commit; caretaker's state machine will pick "
            "up the result from the usual `caretaker:result` markers._",
        ]
    )
    return "\n".join(lines)


class ClaudeCodeExecutor:
    """Hand-off executor that steers tasks at ``anthropics/claude-code-action``.

    The executor conforms to the same ``async run(task, pr) -> ExecutorResult``
    shape as :class:`caretaker.foundry.executor.FoundryExecutor`, so
    :class:`~caretaker.foundry.dispatcher.ExecutorDispatcher` can route to
    either without special-casing.
    """

    def __init__(
        self,
        *,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: ClaudeCodeExecutorConfig,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config

    @property
    def config(self) -> ClaudeCodeExecutorConfig:
        return self._config

    async def run(self, task: CodingTask, pr: PullRequest) -> ExecutorResult:
        """Apply the trigger label + post the hand-off comment.

        Returns :class:`ExecutorResult` with outcome:

        * ``COMPLETED`` — label applied and comment posted successfully.
        * ``ESCALATED`` — attempt cap hit, caller should escalate to Copilot.
        * ``FAILED``    — GitHub API error; caller should escalate.
        """
        if not self._config.enabled:
            logger.debug("ClaudeCodeExecutor.run called while config.enabled=False; escalating")
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason="claude_code.enabled=False",
            )

        # Attempt cap. We count prior hand-offs via the marker comment on
        # the PR; a GH API call per dispatch is fine because this path is
        # low-frequency.
        attempt = await self._count_prior_handoffs(pr.number) + 1
        if attempt > self._config.max_attempts:
            logger.info(
                "Claude Code hand-off capped on PR #%s (attempt %d > max %d); escalating",
                pr.number,
                attempt,
                self._config.max_attempts,
            )
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=(
                    f"claude_code attempt cap hit ({attempt} > "
                    f"{self._config.max_attempts}); escalating to Copilot"
                ),
            )

        body = _build_handoff_comment(
            mention=self._config.mention,
            task=task,
            attempt=attempt,
            max_attempts=self._config.max_attempts,
        )

        try:
            comment = await self._github.add_issue_comment(self._owner, self._repo, pr.number, body)
        except Exception as exc:  # defensive; escalate rather than raise
            logger.exception("ClaudeCodeExecutor: add_issue_comment failed: %s", exc)
            return ExecutorResult(
                outcome=ExecutorOutcome.FAILED,
                reason=f"claude_code comment failed: {exc}",
            )

        try:
            await self._github.add_labels(
                self._owner, self._repo, pr.number, [self._config.trigger_label]
            )
        except Exception as exc:
            # Comment is already posted; the upstream action may still
            # pick up via the mention. Log and continue — treat as COMPLETED
            # but note the label failure in the reason.
            logger.warning(
                "ClaudeCodeExecutor: add_labels(%s) failed on PR #%s: %s",
                self._config.trigger_label,
                pr.number,
                exc,
            )
            return ExecutorResult(
                outcome=ExecutorOutcome.COMPLETED,
                reason=(f"claude_code dispatched via mention (label apply failed: {exc})"),
                comment_id=getattr(comment, "id", None),
                iterations=attempt,
            )

        return ExecutorResult(
            outcome=ExecutorOutcome.COMPLETED,
            reason=f"claude_code dispatched (attempt {attempt}/{self._config.max_attempts})",
            comment_id=getattr(comment, "id", None),
            iterations=attempt,
        )

    async def _count_prior_handoffs(self, pr_number: int) -> int:
        """Return the number of caretaker claude-code hand-off comments on the PR."""
        try:
            comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        except Exception as exc:
            logger.debug("ClaudeCodeExecutor: could not list comments: %s", exc)
            return 0
        return sum(1 for c in comments if CLAUDE_CODE_HANDOFF_MARKER in (c.body or ""))


__all__ = [
    "CLAUDE_CODE_HANDOFF_MARKER",
    "ClaudeCodeExecutor",
]
