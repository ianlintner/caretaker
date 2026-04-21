"""Executor dispatcher — picks between Foundry and Copilot per task.

Existing agent bridges ask the dispatcher for a :class:`RouteResult` for each
task. The dispatcher's decision tree (highest-priority first):

0. **Label override** on the host PR / issue (see :data:`ROUTING_LABELS`):
   * ``agent:quarantine`` — refuse dispatch entirely (``RouteOutcome.REFUSED``).
   * ``agent:custom``     — force the custom executor (Foundry today).
   * ``agent:copilot``    — force the legacy Copilot path.
1. Config ``provider == "copilot"`` → always post the Copilot task (legacy).
2. Config ``provider == "foundry"`` → try Foundry; fall back to Copilot on
   ``ESCALATED`` or ``FAILED``.
3. Config ``provider == "auto"`` → try Foundry when eligible (task type
   allowed and provider credentials present), else Copilot.

The dispatcher is a thin coordinator — it never opens a workspace itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.foundry.executor import CodingTask, ExecutorOutcome, ExecutorResult
from caretaker.llm.copilot import CopilotTask, TaskType

if TYPE_CHECKING:
    from caretaker.config import ExecutorConfig
    from caretaker.foundry.executor import FoundryExecutor
    from caretaker.github_client.models import Comment, PullRequest
    from caretaker.llm.copilot import CopilotProtocol

logger = logging.getLogger(__name__)


class RouteOutcome(StrEnum):
    """How a task was actually dispatched."""

    FOUNDRY = "FOUNDRY"  # Foundry handled it end-to-end
    COPILOT = "COPILOT"  # Copilot comment was posted (legacy path)
    COPILOT_FALLBACK = "COPILOT_FALLBACK"  # Foundry escalated; Copilot posted
    REFUSED = "REFUSED"  # Label-based quarantine refused dispatch


# Canonical routing labels. Operators can apply these to an issue / PR to
# override caretaker's default router decision. Using plain strings (not an
# enum) so downstream repos can reuse the constants without importing from
# inside ``caretaker.foundry``.
LABEL_AGENT_CUSTOM = "agent:custom"
LABEL_AGENT_COPILOT = "agent:copilot"
LABEL_AGENT_QUARANTINE = "agent:quarantine"
ROUTING_LABELS = frozenset({LABEL_AGENT_CUSTOM, LABEL_AGENT_COPILOT, LABEL_AGENT_QUARANTINE})


def _label_names(labels: object) -> set[str]:
    """Best-effort label extraction. Accepts list[str] | list[dict] | list[Label]."""
    if not labels:
        return set()
    names: set[str] = set()
    try:
        iterator = iter(labels)  # type: ignore[call-overload]
    except TypeError:
        return names
    for label in iterator:
        if isinstance(label, str):
            names.add(label)
        else:
            name = getattr(label, "name", None)
            if isinstance(name, str):
                names.add(name)
                continue
            if isinstance(label, dict):
                val = label.get("name")
                if isinstance(val, str):
                    names.add(val)
    return names


def routing_override(labels: object) -> str | None:
    """Return the routing override dictated by labels, or ``None``.

    Precedence is: quarantine > custom > copilot. Caller decides how to
    act on each value; see :class:`ExecutorDispatcher.route`.
    """
    names = _label_names(labels)
    if LABEL_AGENT_QUARANTINE in names:
        return LABEL_AGENT_QUARANTINE
    if LABEL_AGENT_CUSTOM in names:
        return LABEL_AGENT_CUSTOM
    if LABEL_AGENT_COPILOT in names:
        return LABEL_AGENT_COPILOT
    return None


@dataclass
class RouteResult:
    outcome: RouteOutcome
    # Set when a Copilot task comment was posted.
    copilot_comment: Comment | None = None
    # Set when Foundry actually ran (either COMPLETED or ESCALATED).
    foundry_result: ExecutorResult | None = None
    reason: str = ""
    errors: list[str] = field(default_factory=list)


class ExecutorDispatcher:
    """Routes tasks between the Foundry executor and the Copilot protocol."""

    def __init__(
        self,
        *,
        config: ExecutorConfig,
        foundry_executor: FoundryExecutor | None,
        copilot_protocol: CopilotProtocol,
    ) -> None:
        self._config = config
        self._foundry = foundry_executor
        self._copilot = copilot_protocol

    @property
    def provider(self) -> str:
        return self._config.provider

    def foundry_eligible(self, coding_task: CodingTask) -> bool:
        """Return True if the task is a candidate for Foundry routing.

        Callers that only have a :class:`CopilotTask` can build an equivalent
        :class:`CodingTask` and pass it here.
        """
        if self._foundry is None:
            return False
        if not self._config.foundry.enabled:
            return False
        return coding_task.task_type.value in self._config.foundry.allowed_task_types

    async def route(
        self,
        *,
        pr: PullRequest,
        copilot_task: CopilotTask,
        coding_task: CodingTask | None = None,
        labels: object = None,
    ) -> RouteResult:
        """Dispatch a task, handling provider selection + Copilot fallback.

        ``copilot_task`` is required so the Copilot path (legacy or fallback)
        has the exact payload it needs.  ``coding_task`` — if supplied — is
        handed to the Foundry executor; otherwise a CodingTask is derived from
        ``copilot_task``. ``labels`` are the labels currently applied to the
        host PR / issue and participate in the routing decision per
        :data:`ROUTING_LABELS`.
        """
        effective_task = coding_task or self._to_coding_task(copilot_task)

        # 0. Label overrides trump every config knob. Operators use these
        #    to force a specific path on a per-item basis (and to hard-stop
        #    dispatch on a hostile or confusing issue via quarantine).
        override = routing_override(labels if labels is not None else self._pr_labels(pr))
        if override == LABEL_AGENT_QUARANTINE:
            logger.info("dispatch refused by agent:quarantine label on PR #%s", pr.number)
            return RouteResult(
                outcome=RouteOutcome.REFUSED,
                reason="agent:quarantine label present",
            )
        if override == LABEL_AGENT_CUSTOM:
            if self._foundry is None or not self._config.foundry.enabled:
                logger.warning(
                    "agent:custom label on PR #%s but custom executor "
                    "unavailable; falling back to Copilot",
                    pr.number,
                )
                return await self._post_copilot(
                    pr,
                    copilot_task,
                    reason="agent:custom label set but custom executor unavailable",
                    is_fallback=True,
                )
            return await self._run_foundry(
                pr, copilot_task, effective_task, reason="agent:custom label"
            )
        if override == LABEL_AGENT_COPILOT:
            return await self._post_copilot(pr, copilot_task, reason="agent:copilot label")

        if self.provider == "copilot" or self._foundry is None:
            return await self._post_copilot(pr, copilot_task, reason="provider=copilot")

        if self.provider == "auto" and not self.foundry_eligible(effective_task):
            return await self._post_copilot(
                pr, copilot_task, reason="auto: task not Foundry-eligible"
            )

        if self.provider == "foundry" and not self._config.foundry.enabled:
            # Misconfiguration: provider set to foundry but feature disabled.
            logger.warning(
                "executor.provider='foundry' but foundry.enabled=False; routing to Copilot"
            )
            return await self._post_copilot(pr, copilot_task, reason="foundry disabled")

        return await self._run_foundry(
            pr, copilot_task, effective_task, reason=f"provider={self.provider}"
        )

    async def _run_foundry(
        self,
        pr: PullRequest,
        copilot_task: CopilotTask,
        effective_task: CodingTask,
        *,
        reason: str,
    ) -> RouteResult:
        """Invoke the Foundry executor and handle the escalation/failure fallback."""
        assert self._foundry is not None
        try:
            foundry_result = await self._foundry.run(effective_task, pr)
        except Exception as exc:  # defensive; executor itself shouldn't raise
            logger.exception("FoundryExecutor.run raised: %s", exc)
            return await self._post_copilot(
                pr, copilot_task, reason=f"foundry raised: {exc}", is_fallback=True
            )

        if foundry_result.outcome == ExecutorOutcome.COMPLETED:
            return RouteResult(
                outcome=RouteOutcome.FOUNDRY,
                foundry_result=foundry_result,
                reason=f"{reason}: foundry completed",
            )

        # ESCALATED / FAILED → fall back to Copilot.
        logger.info(
            "Foundry outcome=%s reason=%s — falling back to Copilot",
            foundry_result.outcome,
            foundry_result.reason,
        )
        fallback_task = self._augment_copilot_task(copilot_task, foundry_result)
        return await self._post_copilot(
            pr,
            fallback_task,
            reason=f"{reason}: foundry {foundry_result.outcome.value}: {foundry_result.reason}",
            is_fallback=True,
            foundry_result=foundry_result,
        )

    @staticmethod
    def _pr_labels(pr: PullRequest) -> object:
        """Return whatever label container the PR object carries.

        ``PullRequest.labels`` is currently a ``list[str]`` but callers may
        be running against older fixtures that don't populate it. We defer
        normalisation to :func:`_label_names` so the dispatcher stays
        tolerant of schema drift.
        """
        return getattr(pr, "labels", None)

    async def _post_copilot(
        self,
        pr: PullRequest,
        task: CopilotTask,
        *,
        reason: str,
        is_fallback: bool = False,
        foundry_result: ExecutorResult | None = None,
    ) -> RouteResult:
        try:
            comment = await self._copilot.post_task(pr.number, task)
        except Exception as exc:
            logger.exception("Copilot post_task failed: %s", exc)
            return RouteResult(
                outcome=RouteOutcome.COPILOT_FALLBACK if is_fallback else RouteOutcome.COPILOT,
                copilot_comment=None,
                foundry_result=foundry_result,
                reason=f"copilot post failed: {exc}",
                errors=[str(exc)],
            )
        return RouteResult(
            outcome=RouteOutcome.COPILOT_FALLBACK if is_fallback else RouteOutcome.COPILOT,
            copilot_comment=comment,
            foundry_result=foundry_result,
            reason=reason,
        )

    @staticmethod
    def _to_coding_task(copilot_task: CopilotTask) -> CodingTask:
        """Build a CodingTask from an existing CopilotTask (best-effort)."""
        preferred_command: tuple[str, list[str]] | None = None
        if copilot_task.task_type == TaskType.LINT_FAILURE:
            # Common Python lint command; the executor silently ignores if
            # the argv[0] isn't in the allowlist.
            preferred_command = ("ruff", ["check", "."])
        return CodingTask(
            task_type=copilot_task.task_type,
            job_name=copilot_task.job_name,
            error_output=copilot_task.error_output,
            instructions=copilot_task.instructions,
            context=copilot_task.context,
            preferred_command=preferred_command,
        )

    @staticmethod
    def _augment_copilot_task(base: CopilotTask, foundry_result: ExecutorResult) -> CopilotTask:
        """Append Foundry's escalation context to the Copilot task's context field.

        Giving Copilot visibility into why Foundry escalated lets it skip
        approaches that already failed.
        """
        extra = (
            "\n\n---\n"
            "caretaker-foundry attempted this task and escalated.\n"
            f"- outcome: {foundry_result.outcome.value}\n"
            f"- reason: {foundry_result.reason}\n"
            f"- iterations: {foundry_result.iterations}\n"
        )
        new_context = (base.context + extra) if base.context else extra.strip()
        return CopilotTask(
            task_type=base.task_type,
            job_name=base.job_name,
            error_output=base.error_output,
            instructions=base.instructions,
            attempt=base.attempt,
            max_attempts=base.max_attempts,
            priority=base.priority,
            context=new_context,
        )
