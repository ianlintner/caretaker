"""Bridge from ``/runs/{id}/trigger`` to the existing webhook dispatcher.

Workflows offload agent execution to the backend by calling
``POST /runs/{id}/trigger``. This module:

1. Resolves the GitHub App installation id for the run's repository.
2. Synthesises a :class:`ParsedWebhook` carrying the trigger context so
   the existing :class:`WebhookDispatcher` can run agents without a
   second code path.
3. Installs a logging handler scoped to the dispatch ``asyncio.Task`` so
   every ``caretaker.*`` INFO+ log line is appended to the run's Redis
   stream as a structured :class:`LogEntry`. Frontend SSE clients and
   the runner-side mirror see agent output live with no agent code
   changes.
4. Schedules dispatch in the background and emits ``run finished``
   system events on completion.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.runs.models import LogEntry, LogStream, RunRecord, RunStatus, RunTriggerRequest
from caretaker.runs.store import RunsStore, get_store

if TYPE_CHECKING:
    from caretaker.github_app.installation_tokens import InstallationTokenMinter
    from caretaker.github_app.repo_installation import RepoInstallationResolver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-task log streaming
# ---------------------------------------------------------------------------


_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caretaker_runs_current_run_id", default=None
)
_current_seq: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "caretaker_runs_current_seq", default=None
)


class RunStreamHandler(logging.Handler):
    """Forward INFO+ ``caretaker.*`` logs to the active run's stream.

    Activated only for tasks where :data:`_current_run_id` is set, so
    other request handlers running in the same process never have their
    logs captured. The handler is process-wide (one handler attached to
    the root caretaker logger) but fans out per-task via contextvars.
    """

    def __init__(self, store: RunsStore) -> None:
        super().__init__(level=logging.INFO)
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        run_id = _current_run_id.get()
        if not run_id:
            return
        # Synchronous handler in async code — schedule a fire-and-forget task.
        try:
            seq_holder = _current_seq.get()
            if seq_holder is None:
                return
            seq_holder[0] += 1
            entry = LogEntry(
                seq=seq_holder[0],
                ts=datetime.now(UTC),
                stream=LogStream.STDERR if record.levelno >= logging.WARNING else LogStream.STDOUT,
                data=record.getMessage(),
                tags={
                    "logger": record.name,
                    "level": record.levelname,
                },
            )
            asyncio.create_task(  # noqa: RUF006 — fire-and-forget by design
                self._store.append_log(run_id, entry),
                name=f"runs:append_log:{run_id}:{entry.seq}",
            )
        except Exception:  # pragma: no cover — logging must never raise
            self.handleError(record)


_installed_handler: RunStreamHandler | None = None


def install_log_handler(store: RunsStore) -> None:
    """Install the run-stream log handler on the root ``caretaker`` logger.

    Idempotent — safe to call from app startup. Only one handler is ever
    installed per process.
    """
    global _installed_handler  # noqa: PLW0603
    if _installed_handler is not None:
        return
    handler = RunStreamHandler(store)
    logging.getLogger("caretaker").addHandler(handler)
    _installed_handler = handler


def uninstall_log_handler() -> None:
    """Remove the handler (tests / shutdown)."""
    global _installed_handler  # noqa: PLW0603
    if _installed_handler is not None:
        logging.getLogger("caretaker").removeHandler(_installed_handler)
        _installed_handler = None


# ---------------------------------------------------------------------------
# Synthetic webhook construction
# ---------------------------------------------------------------------------


def _synthetic_webhook(
    *,
    record: RunRecord,
    body: RunTriggerRequest,
    installation_id: int,
) -> ParsedWebhook:
    event_type = body.event_type or _event_for_mode(body.mode or record.mode)
    payload: dict[str, Any] = dict(body.event_payload) if body.event_payload else {}
    payload.setdefault(
        "repository",
        {
            "full_name": record.repository,
            "name": record.repository.split("/", 1)[-1],
            "owner": {"login": record.repository_owner},
        },
    )
    payload.setdefault("installation", {"id": installation_id})
    return ParsedWebhook(
        event_type=event_type,
        delivery_id=f"run:{record.run_id}",
        action=payload.get("action") if isinstance(payload.get("action"), str) else None,
        installation_id=installation_id,
        repository_full_name=record.repository,
        payload=payload,
    )


def _event_for_mode(mode: str) -> str:
    """Pick a synthetic event_type matching the requested run mode.

    Caretaker's :func:`agents_for_event` keys agents off the GitHub event
    name. We map our internal modes to a representative event so the
    same dispatcher path serves trigger-driven runs.
    """
    return {
        "full": "schedule",
        "pr-only": "pull_request",
        "issue-only": "issues",
        "upgrade": "schedule",
        "security": "schedule",
        "deps": "schedule",
        "stale": "schedule",
    }.get(mode, "schedule")


# ---------------------------------------------------------------------------
# Trigger entrypoint registered with caretaker.runs.api
# ---------------------------------------------------------------------------


_resolver: RepoInstallationResolver | None = None
_token_broker: InstallationTokenMinter | None = None
_dispatcher_factory: Any = None  # callable returning the WebhookDispatcher
_event_bus_factory: Any = None  # callable returning the EventBus


def configure(
    *,
    resolver: RepoInstallationResolver | None,
    token_broker: InstallationTokenMinter | None,
    dispatcher_factory: Any,
    event_bus_factory: Any = None,
) -> None:
    """Register collaborators wired by main.py at startup.

    ``event_bus_factory`` is optional for backwards compatibility — when
    omitted, :func:`run_trigger` falls back to its legacy in-process
    asyncio task path. Providing the factory routes triggers through the
    durable event bus so they survive pod restarts.
    """
    global _resolver, _token_broker, _dispatcher_factory, _event_bus_factory  # noqa: PLW0603
    _resolver = resolver
    _token_broker = token_broker
    _dispatcher_factory = dispatcher_factory
    _event_bus_factory = event_bus_factory


def reset() -> None:
    global _resolver, _token_broker, _dispatcher_factory, _event_bus_factory  # noqa: PLW0603
    _resolver = None
    _token_broker = None
    _dispatcher_factory = None
    _event_bus_factory = None


async def run_trigger(record: RunRecord, body: RunTriggerRequest) -> bool:
    """Dispatch the run's agents in the background.

    Returns True when a dispatch task was scheduled; False when dispatch
    was deliberately skipped (App not configured / cap reached / repo
    not installed).
    """
    if _dispatcher_factory is None:
        logger.info(
            "run_trigger: dispatcher not configured run_id=%s",
            record.run_id,
        )
        return False

    if _token_broker is None or _resolver is None:
        logger.info(
            "run_trigger: GitHub App not configured run_id=%s repo=%s",
            record.run_id,
            record.repository,
        )
        return False

    installation_id = await _resolver.get(record.repository)
    if installation_id is None:
        logger.warning(
            "run_trigger: App not installed on %s; skipping dispatch run_id=%s",
            record.repository,
            record.run_id,
        )
        return False

    parsed = _synthetic_webhook(
        record=record,
        body=body,
        installation_id=installation_id,
    )
    dispatcher = _dispatcher_factory()
    store = get_store()

    # Preferred path: publish a ``run_trigger`` event onto the durable
    # event bus so the consumer (this replica or another) drives the
    # actual dispatch + terminal-status persistence. Survives pod
    # restarts; load-balanced across replicas via consumer group.
    if _event_bus_factory is not None:
        try:
            from caretaker.eventbus import DEFAULT_STREAM, run_trigger_event_payload

            bus = _event_bus_factory()
            payload = run_trigger_event_payload(
                parsed=parsed,
                run_id=record.run_id,
                last_seq=record.last_seq,
            )
            await bus.publish(DEFAULT_STREAM, payload)
            logger.info(
                "run_trigger published to event bus run_id=%s repo=%s",
                record.run_id,
                record.repository,
            )
            return True
        except Exception:
            logger.warning(
                "run_trigger event bus publish failed; falling back to in-process dispatch "
                "run_id=%s",
                record.run_id,
                exc_info=True,
            )

    # Fallback path: in-process asyncio task. Non-durable — used when
    # the bus is not configured or its publish raised. Keeps the legacy
    # behaviour available so a Redis blip never strands a run.
    async def _runner() -> None:
        token = _current_run_id.set(record.run_id)
        seq_token = _current_seq.set([record.last_seq + 10])  # leave headroom for system events
        try:
            await dispatcher.dispatch(parsed)
            await store.update_run(
                record.run_id,
                status=RunStatus.SUCCEEDED,
                finished_at=datetime.now(UTC),
                exit_code=0,
            )
            await _emit_terminal(store, record.run_id, "succeeded", 0)
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("run_trigger dispatch failed run_id=%s: %s", record.run_id, exc)
            await store.update_run(
                record.run_id,
                status=RunStatus.FAILED,
                finished_at=datetime.now(UTC),
                exit_code=1,
                summary={"error": str(exc)[:500]},
            )
            await _emit_terminal(store, record.run_id, "failed", 1)
        finally:
            _current_run_id.reset(token)
            _current_seq.reset(seq_token)

    asyncio.create_task(_runner(), name=f"runs:trigger:{record.run_id}")  # noqa: RUF006
    return True


async def _emit_terminal(store: RunsStore, run_id: str, status: str, exit_code: int) -> None:
    rec = await store.get_run(run_id)
    next_seq = (rec.last_seq if rec else 0) + 1
    entry = LogEntry(
        seq=next_seq,
        ts=datetime.now(UTC),
        stream=LogStream.SYSTEM,
        data=f"backend dispatch finished status={status} exit_code={exit_code}",
        tags={"status": status, "exit_code": exit_code},
    )
    await store.append_log(run_id, entry)


__all__ = [
    "RunStreamHandler",
    "configure",
    "install_log_handler",
    "reset",
    "run_trigger",
    "uninstall_log_handler",
]
