"""Publish a synthetic ``workflow_run`` event when a backend-driven run fails.

In the legacy architecture, a failed ``caretaker run`` inside the
consumer's GitHub Actions workflow triggered a sibling ``self-heal``
job (see ``setup-templates/templates/workflows/maintainer.yml``). With
the heavy workflow being decommissioned, that sibling job goes away
too — so we replicate the trigger backend-side: when a run terminates
with a non-zero exit code, we publish onto the event bus a payload
shaped like a ``workflow_run`` webhook so the existing
``self-heal`` agent (mapped via :func:`agents_for_event`) picks it up
through the usual dispatcher path.

Best-effort: this trigger is fire-and-forget. A bus-publish failure
logs but does not raise — losing the trigger means the next reconcile
tick (or next webhook for the same repo) will surface the failure
through the regular agent paths.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.eventbus import DEFAULT_STREAM, EventBus, webhook_event_payload
from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.observability.metrics import record_error

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from caretaker.runs.models import RunRecord


async def publish_self_heal_trigger(
    *,
    bus: EventBus,
    record: RunRecord,
    exit_code: int,
    summary: dict[str, Any] | None = None,
) -> None:
    """Publish a synthetic workflow_run event for a failed run.

    No-op when ``exit_code == 0``. Caller is expected to skip the call
    entirely on success — the explicit guard here is defensive in case
    it gets wired into a path that doesn't filter.
    """
    if exit_code == 0:
        return

    if not record.repository or not record.repository_owner:
        logger.debug("self_heal trigger skipped: run %s has no repository context", record.run_id)
        return

    parsed = ParsedWebhook(
        event_type="workflow_run",
        delivery_id=f"self-heal:{record.run_id}",
        action="completed",
        installation_id=None,  # back-fill from resolver if we want a token; agent looks up its own
        repository_full_name=record.repository,
        payload={
            "action": "completed",
            "workflow_run": {
                "id": record.gh_run_id,
                "name": "Caretaker (backend)",
                "conclusion": "failure",
                "head_branch": record.ref or "main",
                "exit_code": exit_code,
                "summary": summary or {},
                "run_id": record.run_id,
            },
            "repository": {
                "full_name": record.repository,
                "name": record.repository.split("/", 1)[-1],
                "owner": {"login": record.repository_owner},
            },
        },
    )
    try:
        await bus.publish(DEFAULT_STREAM, webhook_event_payload(parsed))
        logger.info(
            "self_heal trigger published run_id=%s repo=%s exit_code=%d",
            record.run_id,
            record.repository,
            exit_code,
        )
    except Exception:
        logger.warning(
            "self_heal trigger publish failed run_id=%s",
            record.run_id,
            exc_info=True,
        )
        record_error(kind="self_heal_trigger_publish")


__all__ = ["publish_self_heal_trigger"]
