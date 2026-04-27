"""Hand-off coding agents — Claude Code, opencode, and friends.

A *hand-off* coding agent does not execute the work itself. It applies a
configurable trigger label to the host PR / issue and posts a structured
``@mention`` comment that an upstream GitHub Action workflow picks up.

The pattern was first introduced for ``anthropics/claude-code-action``;
the same shape works for ``sst/opencode-github-action`` and any future
agent that ships a label-triggered GitHub Action. Concrete subclasses
just supply their name, marker, default label, and default mention.

Each agent owns a unique HTML-comment marker in the body of its hand-off
comment (e.g. ``<!-- caretaker:claude-code-handoff -->``). Markers are
how :meth:`HandoffAgent._count_prior_handoffs` enforces the per-PR
attempt cap; reusing a marker across agents would cause cross-talk.
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
    from caretaker.coding_agents.protocol import ExecutionMode
    from caretaker.config import HandoffAgentConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


CLAUDE_CODE_HANDOFF_MARKER = "<!-- caretaker:claude-code-handoff -->"
OPENCODE_HANDOFF_MARKER = "<!-- caretaker:opencode-handoff -->"


class HandoffAgent:
    """Base implementation of the hand-off pattern.

    Subclasses set the four class-level attributes below and inherit the
    full :meth:`run` flow. The hand-off comment body is parameterised by
    ``mention`` (so each agent gets the trigger string the upstream action
    expects) and ``trigger_label`` (so consumer-repo workflows can listen
    on a name they choose).
    """

    # Subclass-supplied identity. Overridden in concrete subclasses.
    name: str = ""
    mode: ExecutionMode = "handoff"
    marker: str = ""
    upstream_action_name: str = ""

    # Default config values used when an operator hasn't overridden them
    # in ``executor.agents.<name>``. Mirrors :class:`HandoffAgentConfig`.
    default_trigger_label: str = ""
    default_mention: str = ""

    def __init__(
        self,
        *,
        github: GitHubClient,
        owner: str,
        repo: str,
        config: HandoffAgentConfig,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def trigger_label(self) -> str:
        return self._config.trigger_label or self.default_trigger_label

    @property
    def mention(self) -> str:
        return self._config.mention or self.default_mention

    @property
    def max_attempts(self) -> int:
        return self._config.max_attempts

    @property
    def config(self) -> HandoffAgentConfig:
        return self._config

    async def run(self, task: CodingTask, pr: PullRequest) -> ExecutorResult:
        """Apply the trigger label + post the hand-off comment.

        Returns :class:`ExecutorResult` with outcome:

        * ``COMPLETED`` — label applied and comment posted successfully.
        * ``ESCALATED`` — attempt cap hit, caller should escalate to Copilot.
        * ``FAILED``    — GitHub API error; caller should escalate.
        """
        if not self._config.enabled:
            logger.debug(
                "%sAgent.run called while config.enabled=False; escalating",
                self.name,
            )
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=f"{self.name}.enabled=False",
            )

        attempt = await self._count_prior_handoffs(pr.number) + 1
        if attempt > self.max_attempts:
            logger.info(
                "%s hand-off capped on PR #%s (attempt %d > max %d); escalating",
                self.name,
                pr.number,
                attempt,
                self.max_attempts,
            )
            return ExecutorResult(
                outcome=ExecutorOutcome.ESCALATED,
                reason=(
                    f"{self.name} attempt cap hit ({attempt} > "
                    f"{self.max_attempts}); escalating to Copilot"
                ),
            )

        body = self._build_handoff_comment(task=task, attempt=attempt)

        try:
            comment = await self._github.add_issue_comment(self._owner, self._repo, pr.number, body)
        except Exception as exc:
            logger.exception("%sAgent: add_issue_comment failed: %s", self.name, exc)
            return ExecutorResult(
                outcome=ExecutorOutcome.FAILED,
                reason=f"{self.name} comment failed: {exc}",
            )

        try:
            await self._github.add_labels(self._owner, self._repo, pr.number, [self.trigger_label])
        except Exception as exc:
            # Comment is already posted; the upstream action may still
            # pick up via the mention. Log and continue — treat as COMPLETED
            # but note the label failure in the reason.
            logger.warning(
                "%sAgent: add_labels(%s) failed on PR #%s: %s",
                self.name,
                self.trigger_label,
                pr.number,
                exc,
            )
            return ExecutorResult(
                outcome=ExecutorOutcome.COMPLETED,
                reason=(f"{self.name} dispatched via mention (label apply failed: {exc})"),
                comment_id=getattr(comment, "id", None),
                iterations=attempt,
            )

        return ExecutorResult(
            outcome=ExecutorOutcome.COMPLETED,
            reason=f"{self.name} dispatched (attempt {attempt}/{self.max_attempts})",
            comment_id=getattr(comment, "id", None),
            iterations=attempt,
        )

    def _build_handoff_comment(self, *, task: CodingTask, attempt: int) -> str:
        """Format the structured hand-off comment the upstream action consumes."""
        lines = [
            self.marker,
            f"{self.mention} caretaker is handing this task off to {self.name}.",
            "",
            f"**task_type**: `{task.task_type.value}`",
            f"**job**: `{task.job_name}`",
            f"**attempt**: {attempt}/{self.max_attempts}",
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
        action_note = (
            f"_Applied by caretaker's {self.name} agent. The upstream "
            f"`{self.upstream_action_name}` workflow will produce the fix as "
            "its own commit; caretaker's state machine will pick up the "
            "result from the usual `caretaker:result` markers._"
        )
        lines.extend(["", action_note])
        return "\n".join(lines)

    async def _count_prior_handoffs(self, pr_number: int) -> int:
        """Return the number of caretaker hand-off comments on the PR for this agent."""
        try:
            comments = await self._github.get_pr_comments(self._owner, self._repo, pr_number)
        except Exception as exc:
            logger.debug("%sAgent: could not list comments: %s", self.name, exc)
            return 0
        return sum(1 for c in comments if self.marker in (c.body or ""))


class ClaudeCodeAgent(HandoffAgent):
    """Hand-off agent for ``anthropics/claude-code-action``."""

    name = "claude_code"
    marker = CLAUDE_CODE_HANDOFF_MARKER
    upstream_action_name = "anthropics/claude-code-action"
    default_trigger_label = "claude-code"
    default_mention = "@claude"


class OpenCodeAgent(HandoffAgent):
    """Hand-off agent for ``sst/opencode``-style GitHub Actions.

    opencode (https://github.com/sst/opencode) supports many providers in
    agent mode (Anthropic, OpenAI, OpenRouter, local models). Caretaker
    treats it as a peer of Claude Code: same hand-off shape, different
    label / mention / marker so attempts don't cross-count and each
    upstream workflow can listen on its own trigger.
    """

    name = "opencode"
    marker = OPENCODE_HANDOFF_MARKER
    upstream_action_name = "sst/opencode/github"
    default_trigger_label = "opencode"
    default_mention = "@opencode-agent"


__all__ = [
    "CLAUDE_CODE_HANDOFF_MARKER",
    "OPENCODE_HANDOFF_MARKER",
    "ClaudeCodeAgent",
    "HandoffAgent",
    "OpenCodeAgent",
]
