"""Long-running consumer that pulls events off the bus and dispatches.

One instance is started per FastAPI replica during the lifespan startup
phase. Each replica registers as a unique consumer in the same consumer
group, so Redis automatically load-balances messages across them. When a
replica dies mid-message, the reaper task (started alongside the
consumer) re-issues the message after the idle threshold, so no work is
lost.

Two event *kinds* share the stream:

* ``webhook`` — a parsed GitHub webhook delivery; the consumer feeds it
  to the :class:`WebhookDispatcher` exactly as the in-process path used
  to.
* ``run_trigger`` — a streamed-runs ``/runs/{id}/trigger`` invocation;
  the consumer sets the run-scoped contextvars (so agent logs stream back
  to the right run) and writes terminal status to the runs store on
  completion.

Keeping both kinds on one stream means a single consumer group does the
load-balancing for everything — no separate worker pools to operate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.observability.metrics import (
    record_error,
    record_webhook_event,
    set_worker_queue_depth,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from caretaker.eventbus.base import Event, EventBus, EventHandler
    from caretaker.github_app.dispatcher import WebhookDispatcher


# Stream / group / claim defaults. Overridable via env so ops can tune
# without a redeploy.
DEFAULT_STREAM = "caretaker:events"
DEFAULT_GROUP = "agents"
DEFAULT_CLAIM_IDLE_MS = 300_000  # 5 min — after this a stuck message gets reclaimed
DEFAULT_REAPER_INTERVAL_SECONDS = 60.0


# ── Payload schema ─────────────────────────────────────────────────────


_PAYLOAD_KIND_WEBHOOK = "webhook"
_PAYLOAD_KIND_RUN_TRIGGER = "run_trigger"


def webhook_event_payload(parsed: ParsedWebhook) -> dict[str, object]:
    """Serialise a :class:`ParsedWebhook` into a bus-publishable dict.

    Stamps W3C ``traceparent``/``tracestate`` from the active OTel
    context so the consumer (potentially on another replica) can
    rejoin the trace via :func:`extracted_context`. No-op when OTel
    isn't configured.
    """
    payload: dict[str, object] = {
        "kind": _PAYLOAD_KIND_WEBHOOK,
        "delivery_id": parsed.delivery_id,
        "event_type": parsed.event_type,
        "action": parsed.action,
        "installation_id": parsed.installation_id,
        "repository_full_name": parsed.repository_full_name,
        "raw_payload": parsed.payload,
    }
    _inject_trace_context(payload)
    return payload


def run_trigger_event_payload(
    *,
    parsed: ParsedWebhook,
    run_id: str,
    last_seq: int,
) -> dict[str, object]:
    """Serialise a streamed-runs trigger into a bus-publishable dict.

    The consumer treats ``run_trigger`` events almost identically to
    webhook events, with the addition of run-scoped log streaming and
    terminal-status persistence on completion. The active OTel context
    is stamped onto the payload (``traceparent``/``tracestate``) so the
    /runs/{id}/trigger root span continues across the bus hop.
    """
    payload: dict[str, object] = {
        "kind": _PAYLOAD_KIND_RUN_TRIGGER,
        "run_id": run_id,
        "last_seq": last_seq,
        "delivery_id": parsed.delivery_id,
        "event_type": parsed.event_type,
        "action": parsed.action,
        "installation_id": parsed.installation_id,
        "repository_full_name": parsed.repository_full_name,
        "raw_payload": parsed.payload,
    }
    _inject_trace_context(payload)
    return payload


def _inject_trace_context(payload: dict[str, object]) -> None:
    """Best-effort W3C trace-context stamp; never raises."""
    try:
        from caretaker.observability import inject_trace_context

        inject_trace_context(payload)
    except Exception:  # pragma: no cover
        pass


def _parsed_from_payload(payload: dict[str, Any]) -> ParsedWebhook | None:
    kind = payload.get("kind")
    if kind not in (_PAYLOAD_KIND_WEBHOOK, _PAYLOAD_KIND_RUN_TRIGGER):
        return None
    try:
        action = payload.get("action")
        installation_id = payload.get("installation_id")
        repo_full = payload.get("repository_full_name")
        raw_payload = payload.get("raw_payload") or {}
        return ParsedWebhook(
            event_type=str(payload["event_type"]),
            delivery_id=str(payload["delivery_id"]),
            action=str(action) if isinstance(action, str) else None,
            installation_id=int(installation_id) if isinstance(installation_id, int) else None,
            repository_full_name=str(repo_full) if isinstance(repo_full, str) else None,
            payload=raw_payload if isinstance(raw_payload, dict) else {},
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("undecodable event payload: %s", exc)
        return None


# ── Consumer-side trace context re-anchor ─────────────────────────────


@contextlib.contextmanager
def _consume_span(
    *,
    kind: object,
    event: Any,
    parsed: ParsedWebhook,
    consumer_name: str,
) -> Any:
    """Open a ``messaging.consume`` span parented on the producer's context.

    Reads the W3C ``traceparent`` the producer stamped onto the payload
    and uses it as the parent context for the consume span. This stitches
    the trace across the Redis Streams hop. No-op when the OTel SDK is
    unavailable.

    Stamps the consume span with semantic-convention messaging
    attributes plus caretaker-specific labels (``delivery_id``,
    ``event_type``) so traces can be filtered to a single GitHub
    delivery in Tempo.
    """
    try:
        from opentelemetry import trace as _trace

        from caretaker.observability import extracted_context
    except Exception:  # pragma: no cover - OTel extra not installed
        yield None
        return

    parent_ctx = extracted_context(event.payload)
    tracer = _trace.get_tracer("caretaker.eventbus")
    kind_str = str(kind) if kind is not None else "unknown"
    with tracer.start_as_current_span(
        f"eventbus.consume {kind_str}",
        context=parent_ctx,
    ) as span:
        try:
            span.set_attribute("messaging.system", "redis")
            span.set_attribute("messaging.operation", "process")
            span.set_attribute("messaging.destination.name", DEFAULT_STREAM)
            span.set_attribute("messaging.consumer.id", consumer_name)
            if getattr(event, "id", None):
                span.set_attribute("messaging.message.id", str(event.id))
            span.set_attribute("caretaker.event_kind", kind_str)
            span.set_attribute("caretaker.delivery_id", parsed.delivery_id)
            span.set_attribute("caretaker.event_type", parsed.event_type)
            if parsed.repository_full_name:
                span.set_attribute("caretaker.repository", parsed.repository_full_name)
            if parsed.installation_id is not None:
                span.set_attribute("caretaker.installation_id", parsed.installation_id)
        except Exception:  # pragma: no cover
            pass
        try:
            yield span
        except Exception as exc:
            try:
                from opentelemetry.trace import Status, StatusCode

                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            except Exception:
                pass
            raise


# ── Consumer name ─────────────────────────────────────────────────────


def _resolve_consumer_name() -> str:
    """Return a stable per-pod consumer name.

    Prefers the Kubernetes pod name (set via downward API as ``HOSTNAME``)
    so each replica has a distinct name in the consumer group. Falls back
    to a process-local name in non-k8s environments.
    """
    explicit = os.environ.get("CARETAKER_EVENT_BUS_CONSUMER", "").strip()
    if explicit:
        return explicit
    pod = os.environ.get("HOSTNAME", "").strip()
    if pod:
        return f"caretaker-{pod}"
    return f"caretaker-{os.getpid()}"


# ── Long-running tasks ────────────────────────────────────────────────


def start_webhook_consumer(
    *,
    bus: EventBus,
    dispatcher: WebhookDispatcher,
    stream: str = DEFAULT_STREAM,
    group: str = DEFAULT_GROUP,
    consumer: str | None = None,
) -> tuple[asyncio.Task[None], asyncio.Task[None]]:
    """Start the consume loop and the reaper. Returns both tasks.

    The caller should keep references to both so they survive garbage
    collection, and cancel them in the FastAPI shutdown hook.
    """
    consumer_name = consumer or _resolve_consumer_name()

    async def handle(event: Event) -> None:
        parsed = _parsed_from_payload(event.payload)
        if parsed is None:
            # Bad payload; do not raise — handler raising means "redeliver",
            # which would just re-poison the PEL with an undecodable
            # message. Drop with a logged error.
            record_error(kind="eventbus_undecodable")
            return

        kind = event.payload.get("kind")

        # Re-anchor the OTel trace at the consumer side using the
        # ``traceparent`` the producer stamped onto the payload. The
        # surrounding span becomes a child of the ``POST /webhooks/github``
        # (or ``/runs/{id}/trigger``) root, even though we're potentially
        # in a different replica. ``_consume_span`` is a no-op context
        # manager when OTel is missing/disabled.
        with _consume_span(
            kind=kind,
            event=event,
            parsed=parsed,
            consumer_name=consumer_name,
        ):
            logger.info(
                "eventbus consume kind=%s event=%s delivery=%s consumer=%s",
                kind,
                parsed.event_type,
                parsed.delivery_id,
                consumer_name,
            )

            if kind == _PAYLOAD_KIND_RUN_TRIGGER:
                await _handle_run_trigger(
                    event=event, parsed=parsed, dispatcher=dispatcher, bus=bus
                )
            else:
                await _handle_webhook(parsed=parsed, dispatcher=dispatcher)

    consume_task = asyncio.create_task(
        bus.consume(
            stream=stream,
            group=group,
            consumer=consumer_name,
            handler=handle,
        ),
        name=f"eventbus-consume:{consumer_name}",
    )

    reaper_task = asyncio.create_task(
        _reaper_loop(
            bus=bus,
            stream=stream,
            group=group,
            consumer=consumer_name,
            handler=handle,
        ),
        name=f"eventbus-reaper:{consumer_name}",
    )

    logger.info(
        "eventbus consumer started stream=%s group=%s consumer=%s",
        stream,
        group,
        consumer_name,
    )
    return consume_task, reaper_task


async def _handle_webhook(*, parsed: ParsedWebhook, dispatcher: WebhookDispatcher) -> None:
    """Webhook flavour: feed parsed delivery to the dispatcher."""
    result = await dispatcher.dispatch(parsed)
    record_webhook_event(
        event=parsed.event_type,
        mode=dispatcher.mode.value,
        outcome=f"bus_{result.outcome}",
    )

    # Server-side fleet touch — keeps /api/admin/fleet live now that the
    # v0.25.0 thin streaming workflow no longer runs ``caretaker run`` in
    # consumer CI (so consumer-side ``emit_heartbeat`` never fires for any
    # fleet repo anymore). Best-effort; failures never propagate.
    if parsed.repository_full_name and result.outcome != "off":
        try:
            from caretaker.fleet import record_dispatch_activity

            await record_dispatch_activity(
                repo=parsed.repository_full_name,
                event_type=parsed.event_type,
                agents_fired=list(result.agents),
                outcome=result.outcome,
            )
        except Exception:
            logger.debug(
                "fleet record_dispatch_activity failed for %s",
                parsed.repository_full_name,
                exc_info=True,
            )

    if result.outcome == "error":
        raise RuntimeError(f"dispatch error: {result.detail or 'unknown'}")


async def _handle_run_trigger(
    *,
    event: Event,
    parsed: ParsedWebhook,
    dispatcher: WebhookDispatcher,
    bus: EventBus,
) -> None:
    """Run-trigger flavour: same dispatch, plus run-scoped log streaming + terminal status.

    Imports the runs subsystem lazily so the consumer remains importable
    in non-runs contexts (tests, slim deployments).

    ``bus`` is reused (not rebuilt) for the failure-path self-heal
    publish to avoid leaking a fresh Redis connection pool per failure.
    """
    from caretaker.runs.dispatch import _current_run_id, _current_seq
    from caretaker.runs.models import RunStatus
    from caretaker.runs.store import get_store

    run_id = str(event.payload.get("run_id", ""))
    last_seq = int(event.payload.get("last_seq", 0) or 0)
    if not run_id:
        logger.error("run_trigger event missing run_id; dropping")
        record_error(kind="eventbus_run_trigger_invalid")
        return

    # Set run-scoped contextvars so caretaker.* log lines emitted during
    # dispatch stream into the right run's Redis stream.
    run_token = _current_run_id.set(run_id)
    seq_token = _current_seq.set([last_seq + 10])  # leave headroom for system events

    store = get_store()
    try:
        await dispatcher.dispatch(parsed)
        await store.update_run(
            run_id,
            status=RunStatus.SUCCEEDED,
            finished_at=datetime.now(UTC),
            exit_code=0,
        )
        await _emit_run_terminal(store=store, run_id=run_id, status="succeeded", exit_code=0)
        record_webhook_event(
            event=parsed.event_type,
            mode=dispatcher.mode.value,
            outcome="bus_run_succeeded",
        )
    except Exception as exc:
        logger.exception("run_trigger dispatch failed run_id=%s: %s", run_id, exc)
        try:
            await store.update_run(
                run_id,
                status=RunStatus.FAILED,
                finished_at=datetime.now(UTC),
                exit_code=1,
                summary={"error": str(exc)[:500]},
            )
            await _emit_run_terminal(store=store, run_id=run_id, status="failed", exit_code=1)
        except Exception:
            logger.warning(
                "run_trigger failed-status persistence failed run_id=%s", run_id, exc_info=True
            )
        # Fire a self-heal trigger via the same bus instance so the
        # self-heal agent picks up the failure through the standard
        # dispatcher path. Reusing the bus avoids leaking a fresh Redis
        # connection pool per failed run.
        try:
            from caretaker.runs.self_heal_trigger import publish_self_heal_trigger

            terminal = await store.get_run(run_id)
            if terminal is not None:
                await publish_self_heal_trigger(
                    bus=bus,
                    record=terminal,
                    exit_code=1,
                    summary={"error": str(exc)[:500]},
                )
        except Exception:
            logger.warning(
                "run_trigger self-heal trigger emit failed run_id=%s",
                run_id,
                exc_info=True,
            )
        record_webhook_event(
            event=parsed.event_type,
            mode=dispatcher.mode.value,
            outcome="bus_run_failed",
        )
        # Persist terminal failure but DO NOT raise — re-running a
        # half-applied agent run via XCLAIM is more dangerous than
        # accepting one failed terminal status. Operators can rerun
        # via /runs manually.
    finally:
        _current_run_id.reset(run_token)
        _current_seq.reset(seq_token)


async def _emit_run_terminal(*, store: Any, run_id: str, status: str, exit_code: int) -> None:
    """Append a system-level terminal log line for the run.

    Delegates to :func:`caretaker.runs.dispatch._emit_terminal` so the
    bus path and the legacy in-process fallback both emit the exact
    same wire-level event (``LogStream.SYSTEM`` + ``status`` /
    ``exit_code`` tags). SSE consumers see one shape for the closing
    marker regardless of which code path drove the run.
    """
    from caretaker.runs.dispatch import _emit_terminal

    await _emit_terminal(store, run_id, status, exit_code)


async def _reaper_loop(
    *,
    bus: EventBus,
    stream: str,
    group: str,
    consumer: str,
    handler: EventHandler,
) -> None:
    """Periodically claim idle PEL messages and re-handle them."""
    raw_idle = os.environ.get("CARETAKER_EVENT_BUS_CLAIM_IDLE_MS", "")
    try:
        min_idle_ms = int(raw_idle) if raw_idle else DEFAULT_CLAIM_IDLE_MS
    except ValueError:
        min_idle_ms = DEFAULT_CLAIM_IDLE_MS

    raw_interval = os.environ.get("CARETAKER_EVENT_BUS_REAPER_INTERVAL_SECONDS", "")
    try:
        interval = float(raw_interval) if raw_interval else DEFAULT_REAPER_INTERVAL_SECONDS
    except ValueError:
        interval = DEFAULT_REAPER_INTERVAL_SECONDS

    while True:
        try:
            handled = await bus.claim_idle(
                stream=stream,
                group=group,
                consumer=consumer,
                min_idle_ms=min_idle_ms,
                handler=handler,
            )
            if handled:
                logger.info(
                    "eventbus reaper reclaimed %d events stream=%s group=%s",
                    handled,
                    stream,
                    group,
                )
                set_worker_queue_depth("eventbus_reclaimed", handled)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("eventbus reaper iteration failed", exc_info=True)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


__all__ = [
    "DEFAULT_GROUP",
    "DEFAULT_STREAM",
    "run_trigger_event_payload",
    "start_webhook_consumer",
    "webhook_event_payload",
]
