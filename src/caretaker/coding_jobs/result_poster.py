from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from caretaker.coding_jobs.models import JobStatus, StatusEvent

if TYPE_CHECKING:
    from caretaker.config import CodingJobsConfig, DiscordConfig
    from caretaker.eventbus.base import Event

logger = logging.getLogger(__name__)

_TERMINAL = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.TIMEOUT,
    JobStatus.DEAD_LETTER,
    JobStatus.HEARTBEAT_TIMEOUT,
}

PostCommentFn = Callable[..., Awaitable[None]]


class ResultPoster:
    def __init__(
        self,
        *,
        post_comment: PostCommentFn,
        config: CodingJobsConfig,
        consumer_name: str = "result-poster-0",
        discord_config: DiscordConfig | None = None,
    ) -> None:
        self._post = post_comment
        self._config = config
        self._consumer_name = consumer_name
        self._discord_config = discord_config

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
        await self._notify_discord(se)

    async def _notify_discord(self, se: StatusEvent) -> None:
        if not self._discord_config:
            return
        from caretaker.notifications.discord import DiscordColor, DiscordNotifier

        notifier = DiscordNotifier.from_config(self._discord_config)
        if not notifier:
            return

        success = se.status == JobStatus.COMPLETED
        color = DiscordColor.SUCCESS if success else DiscordColor.FAILURE
        title = "Coding job completed ✓" if success else f"Coding job failed ({se.status.value})"
        fields: list[dict[str, object]] = [
            {"name": "Job ID", "value": se.job_id or "unknown", "inline": True},
            {"name": "Attempt", "value": str(se.attempt), "inline": True},
        ]
        if se.commit_sha:
            fields.append({"name": "Commit", "value": f"`{se.commit_sha}`", "inline": True})
        if se.error:
            fields.append({"name": "Error", "value": se.error[:512], "inline": False})

        description = ""
        if se.pr_url:
            description = f"[View PR]({se.pr_url})"

        await notifier.send_embed(
            title=title,
            description=description,
            color=color,
            fields=fields,
            url=se.pr_url or None,
        )


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
