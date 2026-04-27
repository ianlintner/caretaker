"""Periodic reconciliation: fan out scheduled runs across the fleet.

In the legacy architecture, every consumer repo's
``.github/workflows/maintainer.yml`` ran a ``5m`` cron that triggered
``caretaker run --mode full`` inside that repo's GitHub Actions. With
the heavy workflow being decommissioned, the responsibility moves
backend-side: this module runs on a sparse interval (default 30 min) and
publishes a synthetic ``schedule`` event onto the event bus per installed
repo.

Multi-pod safety
----------------

Multiple MCP replicas would otherwise each fire a tick. To avoid
fan-out × replica-count duplication, the loop acquires a Redis lease
(``SET caretaker:scheduler:lease NX EX``) before each fan-out. Whichever
pod wins the SETNX runs the tick; others skip until the next interval.
The lease TTL is tuned to be slightly longer than the fan-out itself so
a crash mid-fanout doesn't strand the lease.

When Redis is not configured the lease is skipped — single-pod dev runs
the scheduler unconditionally on every tick.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

from caretaker.eventbus import DEFAULT_STREAM, EventBus
from caretaker.github_app.webhooks import ParsedWebhook
from caretaker.observability.metrics import (
    record_error,
    record_webhook_event,
    set_worker_queue_depth,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import redis.asyncio

    from caretaker.github_app.installations_index import InstallationsIndex


_DEFAULT_INTERVAL_SECONDS = 1_800  # 30 min
_DEFAULT_LEASE_TTL_SECONDS = 60
_LEASE_KEY = "caretaker:scheduler:lease"
_FANOUT_DELIVERY_PREFIX = "scheduler"


class ReconciliationScheduler:
    """Long-running task that fans out periodic ``schedule`` events.

    The scheduler does NOT dispatch agents directly — it publishes onto
    the same event bus the webhook handler uses, so the consumer fleet
    handles the fan-out exactly the same way it handles real GitHub
    webhooks. Same code path, same observability, same retry semantics.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        installations_index: InstallationsIndex,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
        redis_url: str | None = None,
        stream: str = DEFAULT_STREAM,
        instance_id: str | None = None,
    ) -> None:
        self._bus = bus
        self._index = installations_index
        self._interval = interval_seconds
        self._lease_ttl = lease_ttl_seconds
        self._redis_url = redis_url or os.environ.get("REDIS_URL", "").strip()
        self._stream = stream
        self._instance_id = instance_id or os.environ.get("HOSTNAME", "") or str(uuid.uuid4())
        self._redis: redis.asyncio.Redis[str] | None = None
        self._connect_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """Loop until cancelled. Single ``tick()`` per ``interval_seconds``."""
        logger.info(
            "reconciliation scheduler started interval=%.0fs instance=%s",
            self._interval,
            self._instance_id,
        )
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("reconciliation tick failed", exc_info=True)
                record_error(kind="scheduler_tick")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise

    async def tick(self) -> int:
        """Run one fan-out. Returns the number of events published.

        Skips silently when another replica holds the Redis lease.
        """
        if not await self._try_acquire_lease():
            logger.debug("reconciliation tick skipped — another replica holds the lease")
            return 0

        try:
            repos = await self._index.list_repos()
        except Exception:
            logger.warning("installations index lookup failed", exc_info=True)
            record_error(kind="scheduler_index_lookup")
            return 0

        published = 0
        for repo in repos:
            try:
                payload = self._fanout_payload(
                    owner=repo.owner,
                    repo=repo.repo,
                    installation_id=repo.installation_id,
                )
                await self._bus.publish(self._stream, payload)
                published += 1
            except Exception:
                logger.warning(
                    "scheduler publish failed repo=%s/%s",
                    repo.owner,
                    repo.repo,
                    exc_info=True,
                )
                record_error(kind="scheduler_publish")

        set_worker_queue_depth("scheduler_fanout", published)
        record_webhook_event(
            event="schedule",
            mode="active",
            outcome=f"scheduler_fanout_{published}",
        )
        logger.info(
            "reconciliation tick fanned out %d events instance=%s",
            published,
            self._instance_id,
        )
        return published

    # ── Lease ───────────────────────────────────────────────────────

    async def _redis_client(self) -> redis.asyncio.Redis[str] | None:
        if not self._redis_url:
            return None
        if self._redis is not None:
            return self._redis
        async with self._connect_lock:
            if self._redis is None:
                try:
                    import redis.asyncio as aioredis

                    self._redis = aioredis.from_url(
                        self._redis_url,
                        decode_responses=True,
                        socket_connect_timeout=5,
                        socket_timeout=5,
                    )
                except Exception:
                    logger.warning("scheduler: Redis unavailable; running unlocked", exc_info=True)
                    return None
        return self._redis

    async def _try_acquire_lease(self) -> bool:
        client = await self._redis_client()
        if client is None:
            # No Redis → single-pod dev. Always proceed.
            return True
        try:
            return bool(
                await client.set(
                    _LEASE_KEY,
                    self._instance_id,
                    nx=True,
                    ex=self._lease_ttl,
                )
            )
        except Exception:
            logger.warning("scheduler: lease acquire failed; skipping tick", exc_info=True)
            self._redis = None
            return False

    # ── Fanout payload ──────────────────────────────────────────────

    def _fanout_payload(self, *, owner: str, repo: str, installation_id: int) -> dict[str, object]:
        """Construct the synthetic webhook payload published per repo.

        The dispatcher routes by ``event_type``; ``schedule`` is the same
        event the legacy cron used to emit, so the existing
        ``agents_for_event("schedule")`` mapping handles it without any
        additional wiring.
        """
        # Lazily import to avoid a hard dep at module load.
        from caretaker.eventbus import webhook_event_payload

        # Unique-ish delivery id so dedup downstream doesn't collapse two
        # consecutive ticks on the same repo. Includes the instance id
        # so cross-replica audit traces tell you who fired which fan-out.
        delivery_id = (
            f"{_FANOUT_DELIVERY_PREFIX}:{self._instance_id}:{int(time.time())}:{owner}/{repo}"
        )
        parsed = ParsedWebhook(
            event_type="schedule",
            delivery_id=delivery_id,
            action=None,
            installation_id=installation_id,
            repository_full_name=f"{owner}/{repo}",
            payload={
                "scheduler": {
                    "instance": self._instance_id,
                    "interval_seconds": self._interval,
                },
                "repository": {
                    "full_name": f"{owner}/{repo}",
                    "name": repo,
                    "owner": {"login": owner},
                },
                "installation": {"id": installation_id},
            },
        )
        return webhook_event_payload(parsed)


def start_reconciliation_scheduler(
    *,
    bus: EventBus,
    installations_index: InstallationsIndex,
    interval_seconds: float | None = None,
) -> asyncio.Task[None]:
    """Spawn the scheduler as a background task. Returns the task handle.

    The caller is responsible for keeping the handle alive (so it isn't
    garbage collected) and cancelling it on shutdown.
    """
    raw_interval = os.environ.get("CARETAKER_SCHEDULER_INTERVAL_SECONDS", "")
    try:
        env_interval = float(raw_interval) if raw_interval else _DEFAULT_INTERVAL_SECONDS
    except ValueError:
        env_interval = _DEFAULT_INTERVAL_SECONDS
    interval = interval_seconds if interval_seconds is not None else env_interval

    raw_ttl = os.environ.get("CARETAKER_SCHEDULER_LEASE_TTL_SECONDS", "")
    try:
        lease_ttl = int(raw_ttl) if raw_ttl else _DEFAULT_LEASE_TTL_SECONDS
    except ValueError:
        lease_ttl = _DEFAULT_LEASE_TTL_SECONDS

    scheduler = ReconciliationScheduler(
        bus=bus,
        installations_index=installations_index,
        interval_seconds=interval,
        lease_ttl_seconds=lease_ttl,
    )
    return asyncio.create_task(scheduler.run_forever(), name="reconciliation-scheduler")


__all__ = ["ReconciliationScheduler", "start_reconciliation_scheduler"]
