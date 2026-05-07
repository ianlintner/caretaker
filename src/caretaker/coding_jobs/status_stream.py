"""Redis Stream facade for job-status events only.

The coding-tasks queue is handled by AsbCodingQueue.
This module owns the job-status Redis Stream: writes from job containers,
reads from the MCP status endpoint and ResultPoster.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from caretaker.coding_jobs.models import StatusEvent

if TYPE_CHECKING:
    from caretaker.config import CodingJobsConfig
    from caretaker.eventbus.base import EventBus, EventHandler

logger = logging.getLogger(__name__)


class JobStatusStream:
    """Write and read lifecycle events on the job-status Redis Stream."""

    def __init__(self, *, bus: EventBus, config: CodingJobsConfig) -> None:
        self._bus = bus
        self._config = config

    async def write_status(self, event: StatusEvent) -> str:
        return await self._bus.publish(self._config.stream_job_status, event.to_payload())

    async def ensure_consumer_group(self) -> None:
        await self._bus.ensure_group(
            self._config.stream_job_status,
            self._config.status_consumer_group,
        )

    async def consume_results(self, *, consumer: str, handler: EventHandler) -> None:
        """Consume loop for ResultPoster — terminal status events."""
        await self.ensure_consumer_group()
        await self._bus.consume(
            stream=self._config.stream_job_status,
            group=self._config.status_consumer_group,
            consumer=consumer,
            handler=handler,
        )

    async def read_latest_status(self, job_id: str) -> StatusEvent | None:
        """Scan the stream newest-first to find the latest event for job_id."""
        client = await self._bus._get_client()  # type: ignore[attr-defined]
        entries = await client.xrevrange(self._config.stream_job_status, count=200)
        for _entry_id, fields in entries:
            raw = fields.get("payload")
            if not raw:
                continue
            payload = json.loads(raw)
            if payload.get("job_id") == job_id:
                return StatusEvent.from_payload(payload)
        return None


__all__ = ["JobStatusStream"]
