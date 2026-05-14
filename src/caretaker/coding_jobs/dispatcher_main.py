"""Standalone entrypoint for caretaker-job-dispatcher pod."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

logger = logging.getLogger(__name__)


async def _main() -> None:
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient

    from caretaker.coding_jobs.asb_queue import AsbCodingQueue
    from caretaker.coding_jobs.dispatcher import CodingJobDispatcher
    from caretaker.coding_jobs.k8s import build_spawner
    from caretaker.coding_jobs.reconciler import Reconciler
    from caretaker.coding_jobs.result_poster import ResultPoster
    from caretaker.coding_jobs.status_stream import JobStatusStream
    from caretaker.config import CodingJobsConfig, DiscordConfig
    from caretaker.eventbus.redis_streams import RedisStreamsEventBus

    config = CodingJobsConfig(
        enabled=True,
        asb_namespace=os.environ["CARETAKER_ASB_NAMESPACE"],
        k8s_worker_image=os.environ.get("CARETAKER_CODING_JOBS_K8S_WORKER_IMAGE", ""),
    )

    redis_url = os.environ.get("REDIS_URL", "redis://redis.caretaker.svc.cluster.local:6379")
    redis_bus = RedisStreamsEventBus(redis_url=redis_url)
    status_stream = JobStatusStream(bus=redis_bus, config=config)

    credential = DefaultAzureCredential()
    asb_client = ServiceBusClient(
        fully_qualified_namespace=config.asb_namespace,
        credential=credential,
    )
    asb_queue = AsbCodingQueue(config=config, client=asb_client)
    spawner = build_spawner(config)

    reconciler = Reconciler(
        status_stream=status_stream,
        spawner=spawner,
        asb_queue=asb_queue,
        config=config,
    )
    dispatcher = CodingJobDispatcher(
        status_stream=status_stream,
        spawner=spawner,
        asb_queue=asb_queue,
        config=config,
        reconciler=reconciler,
    )

    async def _noop_post(**kwargs: object) -> None:
        logger.info("result-poster: noop post job_id=%s", kwargs.get("job_id"))

    discord_cfg = DiscordConfig(
        enabled=os.environ.get("CARETAKER_DISCORD_BOT_TOKEN", "") != "",
        channel_id=os.environ.get("CARETAKER_DISCORD_CHANNEL_ID", ""),
        bot_token_env="CARETAKER_DISCORD_BOT_TOKEN",
    )
    result_poster = ResultPoster(post_comment=_noop_post, config=config, discord_config=discord_cfg)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)

    tasks = [
        asyncio.create_task(dispatcher.run(asb_client), name="dispatcher"),
        asyncio.create_task(reconciler.run(), name="reconciler"),
        asyncio.create_task(result_poster.run(redis_bus), name="result-poster"),
    ]

    def _on_task_done(task: asyncio.Task[object]) -> None:  # noqa: ANN001
        """Trigger graceful shutdown if a task dies unexpectedly.

        A task that exits without cancellation is a bug — either a Redis
        TimeoutError leaked past the reconnect loop or some other fatal
        condition. Setting ``stop`` here causes the main coroutine to
        cancel the remaining tasks and exit non-zero, which lets
        Kubernetes restart the pod and self-heal.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "task %s failed — triggering shutdown so Kubernetes restarts the pod",
                task.get_name(),
                exc_info=exc,
            )
            stop.set()

    for t in tasks:
        t.add_done_callback(_on_task_done)

    logger.info("caretaker-job-dispatcher started namespace=%s", config.asb_namespace)
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis_bus.close()
    await asb_client.close()
    await credential.close()

    # Exit non-zero when stop was triggered by a task failure (not SIGTERM/SIGINT).
    # Kubernetes restartPolicy:Always applies backoff on non-zero exits, giving
    # operators a clear crash-restart signal rather than a silent zombie pod.
    for t in tasks:
        if not t.cancelled() and t.exception() is not None:
            sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
