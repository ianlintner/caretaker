from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from caretaker.coding_jobs.k8s import K8sJobOutcome
from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.asb_queue import AsbCodingQueue
    from caretaker.coding_jobs.k8s import K8sJobSpawner
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig

logger = logging.getLogger(__name__)

# Exponential backoff: attempt 1→2 = 60s, attempt 2→3 = 180s
_BACKOFF_SECS = {1: 60, 2: 180}
_DEFAULT_BACKOFF = 300


class Reconciler:
    """Polls K8s Job status and heartbeat staleness; schedules ASB retries on failure.

    No XAUTOCLAIM needed — ASB LockDuration=PT5M handles stuck consumers automatically.
    """

    def __init__(
        self,
        *,
        status_stream: JobStatusStream,
        spawner: K8sJobSpawner,
        asb_queue: AsbCodingQueue,
        config: CodingJobsConfig,
    ) -> None:
        self._status = status_stream
        self._spawner = spawner
        self._asb = asb_queue
        self._config = config
        self._in_flight: dict[str, CodingJobMessage] = {}

    def _track(self, msg: CodingJobMessage) -> None:
        self._in_flight[msg.job_id] = msg

    def _untrack(self, job_id: str) -> None:
        self._in_flight.pop(job_id, None)

    async def run(self) -> None:
        while True:
            try:
                await self._check_k8s_jobs()
                heartbeat_map: dict[str, float] = {}
                for job_id in list(self._in_flight):
                    event = await self._status.read_latest_status(job_id)
                    if event is not None:
                        heartbeat_map[job_id] = event.heartbeat_ts
                await self._check_heartbeats(heartbeat_map)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("reconciler: pass failed", exc_info=True)
            await asyncio.sleep(self._config.reconcile_interval_secs)

    async def _check_k8s_jobs(self) -> None:
        for job_name in self._spawner.list_coding_job_names():
            outcome = self._spawner.get_status(job_name)
            job_id = _job_id_from_name(job_name)
            msg = self._in_flight.get(job_id)

            if outcome == K8sJobOutcome.SUCCEEDED:
                self._untrack(job_id)
                continue

            if outcome in (K8sJobOutcome.FAILED, K8sJobOutcome.TIMEOUT):
                status = JobStatus.TIMEOUT if outcome == K8sJobOutcome.TIMEOUT else JobStatus.FAILED
                if msg:
                    await self._status.write_status(
                        StatusEvent(
                            job_id=job_id,
                            status=status,
                            attempt=msg.attempt,
                            error=f"k8s outcome: {outcome.value}",
                        )
                    )
                    self._untrack(job_id)
                    delay = _BACKOFF_SECS.get(msg.attempt, _DEFAULT_BACKOFF)
                    await self._asb.schedule_retry(msg, delay_secs=delay)

    async def _check_heartbeats(self, heartbeat_map: dict[str, float]) -> None:
        threshold = time.time() - self._config.heartbeat_staleness_secs
        for job_id, last_ts in heartbeat_map.items():
            if last_ts < threshold:
                msg = self._in_flight.get(job_id)
                if msg:
                    await self._status.write_status(
                        StatusEvent(
                            job_id=job_id,
                            status=JobStatus.HEARTBEAT_TIMEOUT,
                            attempt=msg.attempt,
                            error=f"no heartbeat for {self._config.heartbeat_staleness_secs}s",
                        )
                    )
                    self._untrack(job_id)
                    delay = _BACKOFF_SECS.get(msg.attempt, _DEFAULT_BACKOFF)
                    await self._asb.schedule_retry(msg, delay_secs=delay)


def _job_id_from_name(job_name: str) -> str:
    # ct-coding-{job_id16}-a{n}
    parts = job_name.split("-")
    return parts[2] if len(parts) >= 4 else ""


__all__ = ["Reconciler"]
