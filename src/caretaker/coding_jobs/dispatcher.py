from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.models import JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.asb_queue import AsbCodingQueue
    from caretaker.coding_jobs.k8s import K8sJobSpawner
    from caretaker.coding_jobs.reconciler import Reconciler
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

_RECEIVE_BATCH = 1  # one at a time — each spawn is heavyweight
_MAX_WAIT_SECS = 5


class CodingJobDispatcher:
    """Peek-lock consumer on the ASB coding-tasks queue. Spawns K8s Jobs."""

    def __init__(
        self,
        *,
        status_stream: JobStatusStream,
        spawner: K8sJobSpawner,
        asb_queue: AsbCodingQueue,
        config: CodingJobsConfig,
        reconciler: Reconciler | None = None,
    ) -> None:
        self._status = status_stream
        self._spawner = spawner
        self._asb = asb_queue
        self._config = config
        self._reconciler = reconciler

    async def run(self, asb_client: Any) -> None:
        """Main consume loop — runs until cancelled."""
        from azure.servicebus import ServiceBusReceiveMode

        logger.info(
            "coding-dispatcher: starting ASB consumer queue=%s", self._config.asb_queue_coding_tasks
        )

        async with asb_client.get_queue_receiver(
            self._config.asb_queue_coding_tasks,
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            max_wait_time=_MAX_WAIT_SECS,
        ) as receiver:
            async for asb_msg in receiver:
                try:
                    await self._handle_message(asb_msg)
                    await receiver.complete_message(asb_msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "coding-dispatcher: handler failed delivery_count=%d — abandoning",
                        asb_msg.delivery_count,
                        exc_info=True,
                    )
                    await receiver.abandon_message(asb_msg)

    async def _handle_message(self, asb_msg: Any) -> None:
        from caretaker.coding_jobs.asb_queue import AsbCodingQueue

        msg = AsbCodingQueue.parse_received(asb_msg)
        logger.info(
            "coding-dispatcher: received job_id=%s attempt=%d delivery_count=%d",
            msg.job_id,
            msg.attempt,
            asb_msg.delivery_count,
        )

        self._spawner.spawn(msg)
        if self._reconciler is not None:
            self._reconciler._track(msg)
        await self._status.write_status(
            StatusEvent(
                job_id=msg.job_id,
                status=JobStatus.SPAWNING,
                attempt=msg.attempt,
                phase="K8S_SPAWN",
            )
        )


__all__ = ["CodingJobDispatcher"]
