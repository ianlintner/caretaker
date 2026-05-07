"""Coding job container entrypoint.

Reads JOB_ID, ATTEMPT, TASK_PAYLOAD (minimal ASB body JSON) from env.
Writes lifecycle events to the job-status Redis Stream via JobStatusStream.
No GitHub API calls — result delivery handled by ResultPoster in dispatcher pod.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING

from caretaker.coding_jobs.models import CodingJobMessage, JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.coding_jobs.status_stream import JobStatusStream

logger = logging.getLogger(__name__)

# Use globals().get so that test patches survive importlib.reload().
HEARTBEAT_INTERVAL_SECS: float = globals().get("HEARTBEAT_INTERVAL_SECS", 30.0)


async def run_coding_worker(*, status_stream: JobStatusStream) -> None:
    body = json.loads(os.environ["TASK_PAYLOAD"])
    attempt = int(os.environ.get("ATTEMPT", "1"))

    # Reconstruct from minimal body + env-injected attempt
    msg = CodingJobMessage(
        job_id=body["job_id"],
        repo=body["repo"],
        task_type=body["task_type"],
        base_sha=body["base_sha"],
        instructions=body["instructions"],
        context=body.get("context", ""),
        attempt=attempt,
    )

    await status_stream.write_status(
        StatusEvent(job_id=msg.job_id, status=JobStatus.STARTED, attempt=msg.attempt, phase="INIT")
    )

    heartbeat_task = asyncio.create_task(_heartbeat_loop(msg, status_stream))

    try:
        commit_sha, pr_url, result_patch = await _run_coding_task(msg)
        await status_stream.write_status(
            StatusEvent(
                job_id=msg.job_id,
                status=JobStatus.COMPLETED,
                attempt=msg.attempt,
                phase="DONE",
                commit_sha=commit_sha,
                pr_url=pr_url,
                result_patch=result_patch,
            )
        )
    except Exception as exc:
        logger.exception("coding-worker: task failed job_id=%s", msg.job_id)
        await status_stream.write_status(
            StatusEvent(
                job_id=msg.job_id,
                status=JobStatus.FAILED,
                attempt=msg.attempt,
                phase="ERROR",
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        raise
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def _heartbeat_loop(msg: CodingJobMessage, stream: JobStatusStream) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECS)
        await stream.write_status(
            StatusEvent(
                job_id=msg.job_id,
                status=JobStatus.RUNNING,
                attempt=msg.attempt,
                phase="HEARTBEAT",
                heartbeat_ts=time.time(),
            )
        )


# Conditionally define _run_coding_task so that test patches survive
# importlib.reload(): if the name is already set (e.g. by a mock), keep it.
_existing_run_coding_task = globals().get("_run_coding_task")
if _existing_run_coding_task is None:

    async def _run_coding_task(msg: CodingJobMessage) -> tuple[str, str, str]:
        """Run coding work via foundry executor. Returns (commit_sha, pr_url, patch)."""
        from caretaker.foundry.executor import CodingTask, FoundryExecutor
        from caretaker.llm.copilot import TaskType

        task = CodingTask(
            task_type=TaskType(msg.task_type),
            job_name=f"k8s-{msg.job_id}",
            error_output="",
            instructions=msg.instructions,
            context=msg.context,
        )
        # FoundryExecutor requires full DI; wiring from env vars is deferred
        # to a factory helper that will be added when the K8s runner is wired up.
        raise NotImplementedError(  # pragma: no cover
            "FoundryExecutor env-based factory not yet implemented; "
            f"task={task!r} executor_class={FoundryExecutor!r}"
        )
else:
    _run_coding_task = _existing_run_coding_task
del _existing_run_coding_task


if __name__ == "__main__":
    import asyncio

    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig
    from caretaker.eventbus.redis_streams import RedisStreamsEventBus

    async def _main() -> None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        config = CodingJobsConfig()
        bus = RedisStreamsEventBus(redis_url=redis_url)
        stream = JobStatusStream(bus=bus, config=config)
        await run_coding_worker(status_stream=stream)

    asyncio.run(_main())
