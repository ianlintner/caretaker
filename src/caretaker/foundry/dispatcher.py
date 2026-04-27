"""Executor dispatcher — picks between BYOCA coding agents and Copilot per task.

Existing agent bridges ask the dispatcher for a :class:`RouteResult` for each
task. The dispatcher's decision tree (highest-priority first):

0. **Label override** on the host PR / issue:

   * ``agent:quarantine`` — refuse dispatch entirely (``RouteOutcome.REFUSED``).
   * ``agent:<name>``     — force the named registered agent (e.g.
     ``agent:opencode``, ``agent:claude_code``). Falls back to Copilot if
     the named agent is unregistered or disabled.
   * ``agent:custom``     — *deprecated* alias for the configured
     ``executor.provider`` agent.
   * ``agent:copilot``    — force the legacy Copilot path.

1. Config ``provider == "copilot"`` → always post the Copilot task (legacy).
2. Config ``provider == "<registered name>"`` → run that agent. On
   ``ESCALATED`` / ``FAILED``, fall back to Copilot. ``foundry`` follows
   the eligibility gate (task type + size); hand-off agents follow their
   per-agent attempt cap.
3. Config ``provider == "auto"`` → try Foundry when eligible, then any
   other enabled custom agent in registration order, then Copilot.

The dispatcher itself never opens a workspace and never calls a model
provider — it just routes. All the per-agent details live in
:mod:`caretaker.coding_agents`.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.foundry.executor import CodingTask, ExecutorOutcome, ExecutorResult
from caretaker.llm.copilot import CopilotTask, TaskType

if TYPE_CHECKING:
    from caretaker.coding_agents.registry import CodingAgentRegistry
    from caretaker.config import ExecutorConfig
    from caretaker.foundry.executor import FoundryExecutor
    from caretaker.github_client.models import Comment, PullRequest
    from caretaker.llm.copilot import CopilotProtocol

logger = logging.getLogger(__name__)


class RouteOutcome(StrEnum):
    """How a task was actually dispatched."""

    FOUNDRY = "FOUNDRY"  # Foundry handled it end-to-end
    CUSTOM_AGENT = "CUSTOM_AGENT"  # any registered hand-off agent dispatched
    # Deprecated: kept for one release so downstream observability that
    # filters on ``CLAUDE_CODE`` keeps working. New routing assigns
    # ``CUSTOM_AGENT`` plus ``agent_name`` instead.
    CLAUDE_CODE = "CLAUDE_CODE"
    COPILOT = "COPILOT"  # Copilot comment was posted (legacy path)
    COPILOT_FALLBACK = "COPILOT_FALLBACK"  # custom executor escalated; Copilot posted
    REFUSED = "REFUSED"  # Label-based quarantine refused dispatch


# Canonical routing labels. Operators can apply these to an issue / PR to
# override caretaker's default router decision. Using plain strings (not an
# enum) so downstream repos can reuse the constants without importing from
# inside ``caretaker.foundry``.
LABEL_AGENT_CUSTOM = "agent:custom"
LABEL_AGENT_COPILOT = "agent:copilot"
LABEL_AGENT_QUARANTINE = "agent:quarantine"
LABEL_AGENT_PREFIX = "agent:"
ROUTING_LABELS = frozenset({LABEL_AGENT_CUSTOM, LABEL_AGENT_COPILOT, LABEL_AGENT_QUARANTINE})

# Reserved agent-label suffixes that don't resolve to a registry entry —
# these are caretaker's hand-rolled overrides and must not collide with a
# registered agent name. Any other ``agent:<x>`` looks up ``x`` in the
# registry.
_RESERVED_AGENT_SUFFIXES = frozenset({"custom", "copilot", "quarantine"})


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

    Precedence is: quarantine > copilot > specific agent (``agent:<name>``)
    > legacy ``custom`` alias. Caller decides how to act on each value;
    see :class:`ExecutorDispatcher.route`.

    Returns either one of the legacy constants
    (:data:`LABEL_AGENT_QUARANTINE` / :data:`LABEL_AGENT_COPILOT` /
    :data:`LABEL_AGENT_CUSTOM`) or a bare ``agent:<name>`` string for the
    caller to resolve against the registry.
    """
    names = _label_names(labels)
    if LABEL_AGENT_QUARANTINE in names:
        return LABEL_AGENT_QUARANTINE
    # Specific ``agent:<name>`` labels (other than reserved suffixes) win
    # over the legacy ``agent:custom`` alias so operators can target a
    # particular registered agent on a per-PR basis. Sorted for
    # determinism when an operator stacks multiple ``agent:<name>``
    # labels (rare; surfaced as a config smell elsewhere).
    for label in sorted(names):
        if not label.startswith(LABEL_AGENT_PREFIX):
            continue
        suffix = label[len(LABEL_AGENT_PREFIX) :]
        if suffix and suffix not in _RESERVED_AGENT_SUFFIXES:
            return label
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
    # Set when any custom executor (Foundry or a hand-off agent) actually ran.
    # Field name preserved for backward compatibility — historically only
    # Foundry populated it. Now also populated by hand-off agents so
    # callers can read ``foundry_result.outcome`` uniformly.
    foundry_result: ExecutorResult | None = None
    reason: str = ""
    errors: list[str] = field(default_factory=list)
    # When ``outcome == CUSTOM_AGENT`` (or the deprecated CLAUDE_CODE
    # alias), the registered agent name that handled the task. Empty for
    # Foundry, Copilot, refusal.
    agent_name: str = ""


class ExecutorDispatcher:
    """Routes tasks between registered coding agents and the Copilot protocol.

    The dispatcher does NOT instantiate agents — they're constructed by
    :meth:`Orchestrator._build_executor_dispatcher` and passed in via the
    ``registry`` argument. Foundry remains a separate parameter (rather
    than a registry entry) because its eligibility check is more
    elaborate than the registry's simple ``enabled`` flag — it inspects
    task type and per-task size budget. The dispatcher promotes Foundry
    to the registry surface only for the ``auto`` provider's fallback
    chain.
    """

    def __init__(
        self,
        *,
        config: ExecutorConfig,
        foundry_executor: FoundryExecutor | None,
        copilot_protocol: CopilotProtocol,
        registry: CodingAgentRegistry | None = None,
        claude_code_executor: object = None,
    ) -> None:
        self._config = config
        self._foundry = foundry_executor
        self._copilot = copilot_protocol
        # Back-compat: tests and older callers passed
        # ``claude_code_executor=...`` directly. Build a single-entry
        # registry so they keep working through the deprecation window.
        if registry is None:
            from caretaker.coding_agents.registry import CodingAgentRegistry as _Registry

            registry = _Registry()
            if claude_code_executor is not None:
                # NOTE: this branch *mutates the caller's object* so a
                # bare ``MagicMock()`` (which is what every legacy test
                # passes) satisfies the registry's ``name`` / ``enabled``
                # contract. Real ``ClaudeCodeAgent`` instances already
                # carry the correct values so the assignments are no-ops
                # on them. The mutation only matters for test-only mocks
                # and is bounded to the deprecation window — production
                # callers should switch to the ``registry=`` argument
                # which avoids the in-place mutation entirely. See
                # CHANGELOG / UPGRADE_GUIDE for the deprecation timeline.
                with contextlib.suppress(Exception):
                    claude_code_executor.name = "claude_code"  # type: ignore[attr-defined]
                with contextlib.suppress(Exception):
                    if not isinstance(getattr(claude_code_executor, "enabled", None), bool):
                        claude_code_executor.enabled = config.claude_code.enabled  # type: ignore[attr-defined]
                registry.register(claude_code_executor)  # type: ignore[arg-type]
        self._registry = registry

    @property
    def provider(self) -> str:
        return self._config.provider

    @property
    def registry(self) -> CodingAgentRegistry:
        return self._registry

    def foundry_eligible(self, coding_task: CodingTask) -> bool:
        """Return True if the task is a candidate for Foundry routing."""
        if self._foundry is None:
            return False
        if not self._config.foundry.enabled:
            return False
        return coding_task.task_type.value in self._config.foundry.allowed_task_types

    def agent_eligible(self, name: str) -> bool:
        """Return True if a registered hand-off agent is available and enabled."""
        agent = self._registry.get(name)
        return agent is not None and agent.enabled

    # Back-compat alias used by older callers / tests.
    def claude_code_eligible(self) -> bool:
        return self.agent_eligible("claude_code")

    def _custom_executor_available(self) -> bool:
        """Is at least one non-Copilot executor wired up?"""
        if self._foundry is not None and self._config.foundry.enabled:
            return True
        return bool(self._registry.enabled())

    async def route(
        self,
        *,
        pr: PullRequest,
        copilot_task: CopilotTask,
        coding_task: CodingTask | None = None,
        labels: object = None,
    ) -> RouteResult:
        """Dispatch a task, handling provider selection + Copilot fallback."""
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
        if override == LABEL_AGENT_COPILOT:
            return await self._post_copilot(pr, copilot_task, reason="agent:copilot label")
        if override and override.startswith(LABEL_AGENT_PREFIX) and override != LABEL_AGENT_CUSTOM:
            target = override[len(LABEL_AGENT_PREFIX) :]
            if not self.agent_eligible(target):
                logger.warning(
                    "%s label on PR #%s but agent %r is not registered/enabled; "
                    "falling back to Copilot",
                    override,
                    pr.number,
                    target,
                )
                return await self._post_copilot(
                    pr,
                    copilot_task,
                    reason=f"{override} label but {target} unavailable",
                    is_fallback=True,
                )
            return await self._run_agent(
                target, pr, copilot_task, effective_task, reason=f"{override} label"
            )
        if override == LABEL_AGENT_CUSTOM:
            return await self._handle_custom_label(pr, copilot_task, effective_task)

        # 1. Provider == copilot — short-circuit to legacy path.
        if self.provider == "copilot":
            return await self._post_copilot(pr, copilot_task, reason="provider=copilot")

        # 2. Provider names a registered agent (claude_code, opencode, …).
        if self._registry.has(self.provider):
            if not self.agent_eligible(self.provider):
                logger.warning(
                    "executor.provider=%r but that agent is disabled or "
                    "unavailable; routing to Copilot",
                    self.provider,
                )
                return await self._post_copilot(
                    pr, copilot_task, reason=f"{self.provider} disabled"
                )
            return await self._run_agent(
                self.provider,
                pr,
                copilot_task,
                effective_task,
                reason=f"provider={self.provider}",
            )

        # 3. Provider == foundry / auto / unknown.
        if self.provider == "foundry":
            if self._foundry is None or not self._config.foundry.enabled:
                logger.warning(
                    "executor.provider='foundry' but foundry.enabled=False; routing to Copilot"
                )
                return await self._post_copilot(pr, copilot_task, reason="foundry disabled")
            return await self._run_foundry(
                pr, copilot_task, effective_task, reason=f"provider={self.provider}"
            )

        if self.provider == "auto":
            if self._foundry is not None and self.foundry_eligible(effective_task):
                return await self._run_foundry(
                    pr, copilot_task, effective_task, reason="auto: foundry eligible"
                )
            # Foundry ineligible — pick the first enabled hand-off agent
            # in registration order. ``_run_agent`` already falls back to
            # Copilot on ESCALATED / FAILED, so a real chain through
            # multiple custom agents would require restructuring that
            # method to not bail to Copilot internally. Phase 1 keeps the
            # legacy single-shot behaviour (Claude Code was always tried
            # alone here) and operators can still target a specific agent
            # via the ``agent:<name>`` PR label.
            primary = next(iter(self._registry.enabled()), None)
            if primary is not None:
                return await self._run_agent(
                    primary.name,
                    pr,
                    copilot_task,
                    effective_task,
                    reason=f"auto: foundry ineligible, {primary.name} eligible",
                )
            return await self._post_copilot(
                pr, copilot_task, reason="auto: no custom agent eligible"
            )

        # Unknown provider name — log and fall back to Copilot. We don't
        # crash the dispatcher because the same config is loaded by
        # multiple agents and a typo shouldn't take the whole orchestrator
        # down. ``caretaker doctor`` surfaces the misconfiguration.
        logger.warning(
            "executor.provider=%r is not registered (known agents: %s); routing to Copilot",
            self.provider,
            ", ".join(self._registry.names()) or "(none)",
        )
        return await self._post_copilot(
            pr, copilot_task, reason=f"unknown provider {self.provider!r}"
        )

    async def _handle_custom_label(
        self,
        pr: PullRequest,
        copilot_task: CopilotTask,
        effective_task: CodingTask,
    ) -> RouteResult:
        """Resolve the deprecated ``agent:custom`` label.

        Preference order: configured ``provider`` (if it names a custom
        agent), then Foundry, then the first enabled hand-off agent.
        Falls back to Copilot when nothing is wired up.
        """
        if not self._custom_executor_available():
            logger.warning(
                "agent:custom label on PR #%s but no custom executor "
                "is enabled; falling back to Copilot",
                pr.number,
            )
            return await self._post_copilot(
                pr,
                copilot_task,
                reason="agent:custom label set but custom executor unavailable",
                is_fallback=True,
            )
        # If provider names a registered agent, prefer it.
        if self.agent_eligible(self.provider):
            return await self._run_agent(
                self.provider, pr, copilot_task, effective_task, reason="agent:custom label"
            )
        if self._foundry is not None and self._config.foundry.enabled:
            return await self._run_foundry(
                pr, copilot_task, effective_task, reason="agent:custom label"
            )
        # Last resort: first enabled registered agent in registration
        # order. ``agent:custom`` is the deprecated alias — operators
        # who want a specific agent should use ``agent:<name>``.
        primary = next(iter(self._registry.enabled()), None)
        if primary is not None:
            return await self._run_agent(
                primary.name, pr, copilot_task, effective_task, reason="agent:custom label"
            )
        return await self._post_copilot(
            pr, copilot_task, reason="agent:custom label but nothing eligible", is_fallback=True
        )

    async def _run_agent(
        self,
        name: str,
        pr: PullRequest,
        copilot_task: CopilotTask,
        effective_task: CodingTask,
        *,
        reason: str,
    ) -> RouteResult:
        """Invoke a registered hand-off agent and handle fallback."""
        agent = self._registry.get(name)
        assert agent is not None, f"agent {name!r} not registered"
        try:
            agent_result = await agent.run(effective_task, pr)
        except Exception as exc:
            logger.exception("%sAgent.run raised: %s", name, exc)
            return await self._post_copilot(
                pr,
                copilot_task,
                reason=f"{name} raised: {exc}",
                is_fallback=True,
            )

        if agent_result.outcome == ExecutorOutcome.COMPLETED:
            # ``CLAUDE_CODE`` is a deprecated alias of ``CUSTOM_AGENT``;
            # we surface the legacy value for ``claude_code`` so existing
            # observability filters keep working through the deprecation
            # window. Switch all consumers to read ``agent_name`` and
            # then drop the alias in a follow-up.
            outcome = (
                RouteOutcome.CLAUDE_CODE if name == "claude_code" else RouteOutcome.CUSTOM_AGENT
            )
            return RouteResult(
                outcome=outcome,
                foundry_result=agent_result,
                reason=f"{reason}: {name} dispatched",
                agent_name=name,
            )

        # ESCALATED / FAILED → fall back to Copilot.
        logger.info(
            "%s outcome=%s reason=%s — falling back to Copilot",
            name,
            agent_result.outcome,
            agent_result.reason,
        )
        fallback_task = self._augment_copilot_task(copilot_task, agent_result)
        return await self._post_copilot(
            pr,
            fallback_task,
            reason=f"{reason}: {name} {agent_result.outcome.value}: {agent_result.reason}",
            is_fallback=True,
            foundry_result=agent_result,
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
                agent_name="foundry",
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
        """Return whatever label container the PR object carries."""
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
        """Append the prior agent's escalation context to the Copilot task's context field.

        Giving Copilot visibility into why the prior agent escalated lets
        it skip approaches that already failed.
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
