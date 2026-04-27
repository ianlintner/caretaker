"""Webhook → agent dispatcher (Phase 2 pilot).

The webhook receiver in :mod:`caretaker.mcp_backend.main` verifies
signatures, dedups deliveries, and logs the event. Phase 2 closes the
loop by handing parsed deliveries to :class:`WebhookDispatcher`, which
decides — based on ``agents_for_event`` and the configured *mode* —
what to do next.

Three modes are supported; they form a safe rollout ladder:

* ``off`` (default) — the dispatcher is bypassed entirely. Existing
  behaviour: log + ack.
* ``shadow`` — the dispatcher records what *would* be dispatched
  (metrics + structured log) but does not run any agent. This is how
  Phase 2 ships so we can observe real traffic — which installations,
  which event types, fan-out — before turning on execution.
* ``active`` — the dispatcher resolves agents and runs them. This mode
  is wired in the follow-up PR once per-installation ``AgentContext``
  construction lands; today it raises ``NotImplementedError`` so a
  misconfiguration fails loud rather than silently running nothing.

Design notes
------------

* **Background execution.** ``dispatch`` is ``async`` but the caller is
  expected to schedule it with ``asyncio.create_task``; the webhook
  endpoint must ack in well under GitHub's 10-second retry budget. The
  dispatcher does not block on agent work.
* **Error isolation.** Each agent invocation is guarded — one agent
  raising never affects siblings. Errors are counted via
  ``caretaker_errors_total{kind="webhook_dispatch"}`` and logged with
  the delivery id.
* **Correlation.** Every log line carries ``delivery_id`` + ``event``
  + ``installation_id`` so a single webhook can be followed across
  fan-out. Correlating back to GitHub's delivery log is one grep.
* **Bounded enums for metric labels.** ``mode`` and ``outcome`` are
  small bounded enums — never arbitrary strings — so the Prometheus
  cardinality stays flat as we add more event types.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from caretaker.github_app.events import agents_for_event
from caretaker.observability.metrics import (
    record_error,
    record_webhook_event,
    record_worker_job,
    set_worker_queue_depth,
)

if TYPE_CHECKING:
    from caretaker.agent_protocol import AgentContext
    from caretaker.github_app.webhooks import ParsedWebhook

logger = logging.getLogger(__name__)


class AgentContextFactory(Protocol):
    """Build an :class:`AgentContext` for a parsed webhook.

    Implementations resolve the installation token, construct a
    ``GitHubClient``, fetch the target repo's maintainer config, and
    open a ``MemoryStore`` — all the plumbing active-mode dispatch needs
    but the dispatcher itself deliberately stays ignorant of. The
    concrete factory lives in a separate module so ``WebhookDispatcher``
    has zero dependency on the GitHub App credential broker at import
    time; ``off`` / ``shadow`` modes never build a context.
    """

    async def build(self, parsed: ParsedWebhook) -> AgentContext: ...


class AgentRunner(Protocol):
    """Execute a single named agent against a built ``AgentContext``.

    The dispatcher doesn't know about ``AgentRegistry`` / ``BaseAgent``
    / ``OrchestratorState`` — it delegates agent execution entirely.
    This keeps the active-mode unit tests free of agent-runtime
    imports.

    Returns a bounded outcome label suitable for Prometheus: one of
    ``"success"``, ``"failure"``, or ``"disabled"``.
    """

    async def run(
        self,
        *,
        agent_name: str,
        context: AgentContext,
        parsed: ParsedWebhook,
    ) -> str: ...


class DispatchMode(StrEnum):
    """Supported dispatcher modes.

    Values match the ``CARETAKER_WEBHOOK_DISPATCH_MODE`` env var so the
    string form is canonical.
    """

    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"

    @classmethod
    def parse(cls, raw: str | None) -> DispatchMode:
        """Parse ``raw`` into a :class:`DispatchMode`, defaulting to ``OFF``.

        Unknown values also resolve to ``OFF`` — the dispatcher should
        never execute an unknown mode.
        """
        if not raw:
            return cls.OFF
        try:
            return cls(raw.strip().lower())
        except ValueError:
            logger.warning("Unknown dispatch mode %r; defaulting to 'off'", raw)
            return cls.OFF


@dataclass(frozen=True)
class DispatchResult:
    """Result envelope returned by :meth:`WebhookDispatcher.dispatch`."""

    mode: DispatchMode
    event: str
    delivery_id: str
    agents: tuple[str, ...]
    outcome: str  # bounded enum — see record_webhook_event docstring
    duration_seconds: float
    detail: str | None = None


# Per-agent soft timeout when active mode is wired up. Shadow mode is
# bounded by the time ``agents_for_event`` takes to return, which is
# sub-microsecond — no timeout required today.
_DEFAULT_AGENT_TIMEOUT_SECONDS: float = 120.0


class WebhookDispatcher:
    """Route parsed webhooks to caretaker agents according to ``mode``.

    The dispatcher is deliberately agent-runtime agnostic: it never
    constructs ``GitHubClient`` or ``AgentContext`` directly. Those
    factories live in the orchestrator and will be injected in the
    follow-up PR that wires :meth:`_run_active`. Keeping the dispatcher
    thin means the ``off`` and ``shadow`` paths have zero dependency
    surface, so the dispatcher itself can be imported safely from the
    webhook endpoint even when the rest of the backend is stubbed.
    """

    def __init__(
        self,
        *,
        mode: DispatchMode = DispatchMode.OFF,
        agent_timeout_seconds: float = _DEFAULT_AGENT_TIMEOUT_SECONDS,
        context_factory: AgentContextFactory | None = None,
        agent_runner: AgentRunner | None = None,
        active_agents: frozenset[str] | None = None,
    ) -> None:
        self._mode = mode
        self._agent_timeout = agent_timeout_seconds
        self._context_factory = context_factory
        self._agent_runner = agent_runner
        # None = all agents resolved by ``agents_for_event`` run in active
        # mode. A set = the allow-list; agents not in it silently fall back
        # to ``shadow`` treatment so we can roll out one agent at a time.
        self._active_agents = active_agents

    @property
    def mode(self) -> DispatchMode:
        return self._mode

    async def dispatch(self, parsed: ParsedWebhook) -> DispatchResult:
        """Process a parsed webhook according to the configured mode.

        Safe to call regardless of mode — callers do not need to branch
        on ``OFF`` before invoking this. Returns a :class:`DispatchResult`
        describing what happened so the caller can log / assert in tests.
        """
        started = time.monotonic()
        agents: tuple[str, ...] = ()

        try:
            agents = tuple(agents_for_event(parsed.event_type))
            if self._mode is DispatchMode.OFF:
                outcome = "off"
                detail = "dispatcher disabled"
            elif not agents:
                outcome = "no_agents"
                detail = "no agents registered for event"
                self._log(parsed, outcome, agents)
            elif self._mode is DispatchMode.SHADOW:
                outcome = "shadow"
                detail = self._run_shadow(parsed, agents)
            elif self._mode is DispatchMode.ACTIVE:
                outcome, detail = await self._run_active(parsed, agents)
            else:  # pragma: no cover — enum exhaustive above
                outcome = "error"
                detail = f"unhandled dispatch mode: {self._mode}"
        except Exception as exc:  # defensive; dispatch must never raise
            logger.exception(
                "webhook dispatch failed event=%s delivery=%s: %s",
                parsed.event_type,
                parsed.delivery_id,
                exc,
            )
            record_error(kind="webhook_dispatch")
            outcome = "error"
            detail = f"dispatch raised: {exc!r}"

        duration = time.monotonic() - started
        record_webhook_event(
            event=parsed.event_type,
            mode=self._mode.value,
            outcome=outcome,
        )
        return DispatchResult(
            mode=self._mode,
            event=parsed.event_type,
            delivery_id=parsed.delivery_id,
            agents=agents,
            outcome=outcome,
            duration_seconds=duration,
            detail=detail,
        )

    # ── Mode implementations ─────────────────────────────────────────

    def _run_shadow(self, parsed: ParsedWebhook, agents: tuple[str, ...]) -> str:
        """Record what *would* be dispatched without touching any agent.

        Shadow mode is the observation pass: we want to see real event
        shapes and fan-out counts before turning on execution. Emits one
        structured log line per would-be agent invocation plus a
        ``worker_job_duration_seconds`` sample labelled ``outcome=shadow``
        so the dashboards are already populated by the time active mode
        lands.
        """
        for agent in agents:
            self._log_agent_would_run(parsed, agent)
            # Zero-duration shadow job — still useful to watch rate / fan-out
            # on the existing worker_jobs_total dashboard panel.
            record_worker_job(job=f"webhook:{agent}", outcome="shadow", duration=0.0)
        self._log(parsed, "shadow", agents)
        return f"shadow-dispatched to {len(agents)} agents"

    async def _run_active(self, parsed: ParsedWebhook, agents: tuple[str, ...]) -> tuple[str, str]:
        """Run resolved agents against a per-installation context.

        Agents in :attr:`_active_agents` (or all, when ``None``) run
        through :attr:`_agent_runner`; others fall back to shadow
        logging so a partial allow-list still produces observability
        for the un-promoted agents during rollout. Each invocation is
        bounded by :attr:`_agent_timeout` and fully error-isolated —
        one failing agent never affects siblings.

        Missing factory / runner is a misconfiguration: raises so
        ``dispatch``'s outer guard records ``outcome="error"`` instead
        of silently doing nothing.
        """
        if self._context_factory is None or self._agent_runner is None:
            raise RuntimeError(
                "active dispatch mode requires both context_factory and "
                "agent_runner — pass them to WebhookDispatcher(...) or "
                "keep mode=shadow until they are wired (see "
                "docs/github-app-phase2.md)."
            )

        context = await self._context_factory.build(parsed)
        ran: list[str] = []
        shadowed: list[str] = []
        failed: list[str] = []

        for agent in agents:
            if self._active_agents is not None and agent not in self._active_agents:
                # Outside the allow-list → shadow. Keeps dashboards
                # populated for un-promoted agents during rollout.
                self._log_agent_would_run(parsed, agent)
                record_worker_job(job=f"webhook:{agent}", outcome="shadow", duration=0.0)
                shadowed.append(agent)
                continue

            outcome = await self._run_one_active_agent(parsed, agent, context)
            if outcome == "success":
                ran.append(agent)
            else:
                failed.append(agent)

        self._log(parsed, "active", agents)
        detail = f"active ran={len(ran)} failed={len(failed)} shadowed={len(shadowed)}"
        outcome = "active" if not failed else "active_partial"
        return outcome, detail

    async def _run_one_active_agent(
        self,
        parsed: ParsedWebhook,
        agent: str,
        context: AgentContext,
    ) -> str:
        """Run a single agent with timeout + error isolation.

        Returns the per-agent outcome label recorded on
        ``worker_jobs_total{job="webhook:<agent>"}``.
        """
        assert self._agent_runner is not None  # guarded by _run_active
        started = time.monotonic()
        outcome = "failure"
        try:
            outcome = await asyncio.wait_for(
                self._agent_runner.run(
                    agent_name=agent,
                    context=context,
                    parsed=parsed,
                ),
                timeout=self._agent_timeout,
            )
        except TimeoutError:
            logger.error(
                "webhook active agent timeout agent=%s event=%s delivery=%s timeout=%.1fs",
                agent,
                parsed.event_type,
                parsed.delivery_id,
                self._agent_timeout,
            )
            record_error(kind="webhook_dispatch")
            outcome = "timeout"
        except Exception as exc:
            logger.exception(
                "webhook active agent failed agent=%s event=%s delivery=%s: %s",
                agent,
                parsed.event_type,
                parsed.delivery_id,
                exc,
            )
            record_error(kind="webhook_dispatch")
            outcome = "failure"
        finally:
            record_worker_job(
                job=f"webhook:{agent}",
                outcome=outcome,
                duration=time.monotonic() - started,
            )
        return outcome

    # ── Logging helpers ─────────────────────────────────────────────

    def _log(
        self,
        parsed: ParsedWebhook,
        outcome: str,
        agents: tuple[str, ...],
    ) -> None:
        logger.info(
            "webhook dispatch mode=%s outcome=%s event=%s action=%s "
            "delivery=%s installation=%s repository=%s agents=%s",
            self._mode.value,
            outcome,
            parsed.event_type,
            parsed.action,
            parsed.delivery_id,
            parsed.installation_id,
            parsed.repository_full_name,
            list(agents),
        )

    def _log_agent_would_run(self, parsed: ParsedWebhook, agent: str) -> None:
        logger.info(
            "webhook shadow would-dispatch agent=%s event=%s action=%s "
            "delivery=%s installation=%s repository=%s",
            agent,
            parsed.event_type,
            parsed.action,
            parsed.delivery_id,
            parsed.installation_id,
            parsed.repository_full_name,
        )


def _max_in_flight() -> int:
    """Bound on concurrent in-flight dispatch tasks.

    Resolved per-call (not cached) so tests and operators can tune via
    ``CARETAKER_WEBHOOK_MAX_IN_FLIGHT`` without restarting the process.
    A non-positive / unparseable value falls back to the default.
    """
    raw = os.environ.get("CARETAKER_WEBHOOK_MAX_IN_FLIGHT", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_IN_FLIGHT
    return value if value > 0 else _DEFAULT_MAX_IN_FLIGHT


_DEFAULT_MAX_IN_FLIGHT: int = 64
_in_flight: set[asyncio.Task[DispatchResult]] = set()


def in_flight_count() -> int:
    """Return the number of dispatch tasks currently in flight (test/observability hook)."""
    return len(_in_flight)


def dispatch_in_background(
    dispatcher: WebhookDispatcher,
    parsed: ParsedWebhook,
) -> asyncio.Task[DispatchResult] | None:
    """Schedule :meth:`WebhookDispatcher.dispatch` as a background task.

    Returns the task so callers can ``await`` it in tests; production
    callers (the webhook endpoint) drop it on the event loop so the
    webhook handler returns immediately.

    To prevent unbounded memory growth when GitHub is in rate-limit
    cooldown — every dispatch waits on the cooldown without releasing
    the parsed payload + agent context — concurrent in-flight tasks are
    capped by ``CARETAKER_WEBHOOK_MAX_IN_FLIGHT`` (default 64). When the
    cap is reached the call returns ``None`` and the delivery is dropped
    on the floor with a counted ``webhook_dispatch_dropped`` error. The
    webhook itself was already 200-acked, and GitHub won't redeliver
    successful acks — operators rely on the ``caretaker_errors_total``
    counter + the ``worker_queue_depth{queue="webhook_dispatch"}`` gauge
    to notice and either scale up replicas or raise the cap.
    """
    cap = _max_in_flight()
    if len(_in_flight) >= cap:
        logger.warning(
            "webhook dispatch dropped (in-flight cap reached) "
            "in_flight=%d cap=%d event=%s delivery=%s",
            len(_in_flight),
            cap,
            parsed.event_type,
            parsed.delivery_id,
        )
        record_error(kind="webhook_dispatch_dropped")
        record_webhook_event(
            event=parsed.event_type,
            mode=dispatcher.mode.value,
            outcome="dropped_overload",
        )
        set_worker_queue_depth("webhook_dispatch", len(_in_flight))
        return None

    task = asyncio.create_task(
        dispatcher.dispatch(parsed),
        name=f"webhook-dispatch:{parsed.delivery_id}",
    )
    _in_flight.add(task)
    set_worker_queue_depth("webhook_dispatch", len(_in_flight))

    def _on_done(t: asyncio.Task[DispatchResult]) -> None:
        _in_flight.discard(t)
        set_worker_queue_depth("webhook_dispatch", len(_in_flight))

    task.add_done_callback(_on_done)
    return task


__all__ = [
    "AgentContextFactory",
    "AgentRunner",
    "DispatchMode",
    "DispatchResult",
    "WebhookDispatcher",
    "dispatch_in_background",
    "in_flight_count",
]
