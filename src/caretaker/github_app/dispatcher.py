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
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.github_app.events import agents_for_event
from caretaker.observability.metrics import (
    record_error,
    record_webhook_event,
    record_worker_job,
)

if TYPE_CHECKING:
    from caretaker.github_app.webhooks import ParsedWebhook

logger = logging.getLogger(__name__)


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
            logger.warning(
                "Unknown dispatch mode %r; defaulting to 'off'", raw
            )
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
    ) -> None:
        self._mode = mode
        self._agent_timeout = agent_timeout_seconds

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

    def _run_shadow(
        self, parsed: ParsedWebhook, agents: tuple[str, ...]
    ) -> str:
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

    async def _run_active(
        self, parsed: ParsedWebhook, agents: tuple[str, ...]
    ) -> tuple[str, str]:
        """Run resolved agents (follow-up PR).

        Intentionally not implemented. Active mode requires a
        per-installation ``AgentContext`` factory — installation token
        from the broker, ``.github/maintainer/config.yml`` fetched from
        the target repo via Contents API, ``MemoryStore`` opened
        against the shared backend, ``GitHubClient`` constructed from
        the installation token — which is a separate, testable piece
        of work. Raising here ensures an operator who sets
        ``CARETAKER_WEBHOOK_DISPATCH_MODE=active`` before that work
        lands gets a loud failure instead of a silent no-op.
        """
        raise NotImplementedError(
            "active dispatch mode is not yet wired — run shadow mode until "
            "per-installation AgentContext construction lands (see "
            "docs/github-app-phase2.md)."
        )

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

    def _log_agent_would_run(
        self, parsed: ParsedWebhook, agent: str
    ) -> None:
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


def dispatch_in_background(
    dispatcher: WebhookDispatcher,
    parsed: ParsedWebhook,
) -> asyncio.Task[DispatchResult]:
    """Schedule :meth:`WebhookDispatcher.dispatch` as a background task.

    Returned so callers can ``await`` it in tests. Production callers
    (the webhook endpoint) drop the task on the event loop so the
    webhook handler returns immediately.

    The task holds a reference via the default loop; callers do not
    need to store it themselves unless they want to await.
    """
    return asyncio.create_task(
        dispatcher.dispatch(parsed),
        name=f"webhook-dispatch:{parsed.delivery_id}",
    )


__all__ = [
    "DispatchMode",
    "DispatchResult",
    "WebhookDispatcher",
    "dispatch_in_background",
]
