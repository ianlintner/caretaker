from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.models import JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.config import CodingJobsConfig
    from caretaker.eventbus.base import Event

logger = logging.getLogger(__name__)

_TERMINAL = {JobStatus.COMPLETED, JobStatus.DEAD_LETTER, JobStatus.HEARTBEAT_TIMEOUT}

PostCommentFn = Callable[..., Awaitable[None]]


class ResultPoster:
    def __init__(
        self,
        *,
        post_comment: PostCommentFn,
        config: CodingJobsConfig,
        consumer_name: str = "result-poster-0",
    ) -> None:
        self._post = post_comment
        self._config = config
        self._consumer_name = consumer_name

    async def run(self, redis_bus: Any) -> None:
        from caretaker.coding_jobs.status_stream import JobStatusStream

        stream = JobStatusStream(bus=redis_bus, config=self._config)
        await stream.consume_results(
            consumer=self._consumer_name, handler=self._handle_status_event
        )

    async def _handle_status_event(self, event: Event) -> None:
        try:
            se = StatusEvent.from_payload(event.payload)
        except (KeyError, ValueError):
            logger.error("result-poster: undecodable event id=%s", event.id)
            return

        if se.status not in _TERMINAL:
            return

        body = _format_success(se) if se.status == JobStatus.COMPLETED else _format_failure(se)
        logger.info("result-poster: posting job_id=%s status=%s", se.job_id, se.status.value)
        await self._post(job_id=se.job_id, body=body)


def _format_success(e: StatusEvent) -> str:
    lines = ["**Coding job completed** ✓", "", f"Commit: `{e.commit_sha}`"]
    if e.pr_url:
        lines.append(f"PR: {e.pr_url}")
    if e.result_patch:
        lines += [
            "",
            "<details><summary>Patch</summary>",
            "",
            f"```diff\n{e.result_patch[:4000]}\n```",
            "</details>",
        ]
    return "\n".join(lines)


def _format_failure(e: StatusEvent) -> str:
    return "\n".join(
        [
            f"**Coding job failed** after {e.attempt} attempt(s).",
            "",
            f"Status: `{e.status.value}`",
            f"Error: {e.error or 'unknown'}",
        ]
    )


__all__ = ["ResultPoster"]
