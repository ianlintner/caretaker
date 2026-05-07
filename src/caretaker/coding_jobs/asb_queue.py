"""Azure Service Bus queue client for the coding-tasks queue.

Body: minimal JSON task spec.
application_properties: traceparent (W3C OTel), job_id, first_enqueued_ts.
Retry, dead-letter, and wall-clock TTL are enforced by ASB configuration
(MaxDeliveryCount=3, DefaultMessageTimeToLive=PT45M) — not in code.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.coding_jobs.models import CodingJobMessage
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

_WALL_CLOCK_TTL_SECS = 2700  # 45 min — matches queue DefaultMessageTimeToLive


class AsbCodingQueue:
    """Send and receive CodingJobMessages via an Azure Service Bus queue."""

    def __init__(self, *, config: CodingJobsConfig, client: Any) -> None:
        self._config = config
        self._client = client

    async def enqueue(self, msg: CodingJobMessage, traceparent: str = "") -> None:
        """Send a new coding task to the ASB queue."""
        from azure.servicebus import ServiceBusMessage

        body = json.dumps(msg.to_asb_body()).encode()
        properties = msg.to_asb_properties(traceparent=traceparent)

        asb_msg = ServiceBusMessage(
            body=body,
            message_id=msg.asb_message_id(),
            time_to_live=timedelta(seconds=_WALL_CLOCK_TTL_SECS),
            application_properties=properties,
        )

        async with self._client.get_queue_sender(self._config.asb_queue_coding_tasks) as sender:
            await sender.send_messages(asb_msg)

        logger.info("asb-queue: enqueued job_id=%s", msg.job_id)

    async def schedule_retry(self, msg: CodingJobMessage, *, delay_secs: int) -> None:
        """Schedule a retry message to be delivered after delay_secs."""
        from azure.servicebus import ServiceBusMessage

        from caretaker.coding_jobs.models import CodingJobMessage

        retry_msg = CodingJobMessage(
            job_id=msg.job_id,
            repo=msg.repo,
            task_type=msg.task_type,
            base_sha=msg.base_sha,
            instructions=msg.instructions,
            context=msg.context,
            first_enqueued_ts=msg.first_enqueued_ts,
            attempt=msg.attempt + 1,
        )

        elapsed = time.time() - msg.first_enqueued_ts
        remaining = _WALL_CLOCK_TTL_SECS - elapsed
        if remaining <= 0:
            logger.info("asb-queue: wall-clock TTL exhausted, dropping retry job_id=%s", msg.job_id)
            return

        body = json.dumps(retry_msg.to_asb_body()).encode()
        properties = retry_msg.to_asb_properties()

        asb_msg = ServiceBusMessage(
            body=body,
            message_id=retry_msg.asb_message_id(),
            time_to_live=timedelta(seconds=remaining),
            application_properties=properties,
        )

        enqueue_time = datetime.now(UTC) + timedelta(seconds=delay_secs)
        async with self._client.get_queue_sender(self._config.asb_queue_coding_tasks) as sender:
            await sender.schedule_messages(asb_msg, enqueue_time)

        logger.info(
            "asb-queue: scheduled retry job_id=%s attempt=%d delay=%ds",
            msg.job_id,
            retry_msg.attempt,
            delay_secs,
        )

    @staticmethod
    def parse_received(asb_msg: Any) -> CodingJobMessage:
        """Parse a received ASB message into a CodingJobMessage."""
        from caretaker.coding_jobs.models import CodingJobMessage as Msg

        raw = b"".join(asb_msg.body)
        body = json.loads(raw)
        props = asb_msg.application_properties or {}
        return Msg.from_asb(
            body=body,
            properties={k.decode() if isinstance(k, bytes) else k: v for k, v in props.items()},
            delivery_count=asb_msg.delivery_count,
        )


def build_asb_queue(config: CodingJobsConfig) -> AsbCodingQueue:
    """Build an AsbCodingQueue using DefaultAzureCredential (workload identity in-cluster)."""
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient

    credential = DefaultAzureCredential()
    client = ServiceBusClient(
        fully_qualified_namespace=config.asb_namespace,
        credential=credential,
    )
    return AsbCodingQueue(config=config, client=client)


__all__ = ["AsbCodingQueue", "build_asb_queue"]
