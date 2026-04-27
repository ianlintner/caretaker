"""Redis-Streams + consumer-group implementation of :class:`EventBus`.

Why Redis Streams: caretaker already runs Redis (token cache, dedup, run
log streams), so adding an event bus on top costs nothing in
infrastructure. Consumer groups give at-least-once delivery,
load-balancing across replicas, and ``XCLAIM`` for redelivering messages
stuck on dead consumers — i.e. exactly the durability and multi-pod
properties we need without dragging in a full message broker.

Wire-level invariants
---------------------

* Producers do ``XADD ... MAXLEN ~ N`` so the stream stays bounded.
* Consumers do ``XREADGROUP GROUP g c COUNT n BLOCK ms STREAMS s >``,
  ack with ``XACK s g id`` only on successful handler return.
* A handler that raises leaves the message in the pending entry list
  (PEL). The reaper (:meth:`claim_idle`) re-issues it via ``XCLAIM``
  once the idle timer elapses, which means a replica that crashes
  mid-handler does not lose the message.
* The producer treats ``EventBusError`` as fatal-by-default; the
  webhook handler upstream catches it and falls back to in-process
  dispatch so a Redis outage does not return 5xx to GitHub.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from caretaker.eventbus.base import Event, EventBusError, EventHandler

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    import redis.asyncio


_PAYLOAD_FIELD = "payload"


class RedisStreamsEventBus:
    """:class:`EventBus` backed by Redis Streams + consumer groups."""

    def __init__(
        self,
        redis_url: str,
        *,
        max_len: int = 100_000,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        self._redis_url = redis_url
        self._max_len = max_len
        self._connect_timeout = connect_timeout_seconds
        self._client: redis.asyncio.Redis[str] | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> redis.asyncio.Redis[str]:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                import redis.asyncio as aioredis

                self._client = aioredis.from_url(
                    self._redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self._connect_timeout,
                    socket_timeout=self._connect_timeout,
                )
        return self._client

    # ── Producer ────────────────────────────────────────────────────

    async def publish(self, stream: str, payload: dict[str, Any]) -> str:
        try:
            client = await self._get_client()
            encoded = json.dumps(payload, separators=(",", ":"), default=str)
            event_id = await client.xadd(
                name=stream,
                fields={_PAYLOAD_FIELD: encoded},
                maxlen=self._max_len,
                approximate=True,
            )
            return str(event_id)
        except Exception as exc:
            self._client = None  # force reconnect
            raise EventBusError(f"publish failed: stream={stream} err={exc!r}") from exc

    # ── Consumer group bootstrap ────────────────────────────────────

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            client = await self._get_client()
            try:
                await client.xgroup_create(
                    name=stream,
                    groupname=group,
                    id="$",
                    mkstream=True,
                )
                logger.info("created consumer group %s on %s", group, stream)
            except Exception as exc:
                # BUSYGROUP -- group already exists. Anything else is a real error.
                if "BUSYGROUP" in str(exc):
                    return
                raise
        except Exception as exc:
            self._client = None
            raise EventBusError(
                f"ensure_group failed: stream={stream} group={group} err={exc!r}"
            ) from exc

    # ── Consumer loop ───────────────────────────────────────────────

    async def consume(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
        block_ms: int = 5_000,
        batch_size: int = 10,
    ) -> None:
        await self.ensure_group(stream, group)
        logger.info(
            "eventbus.consume start stream=%s group=%s consumer=%s",
            stream,
            group,
            consumer,
        )
        client = await self._get_client()

        while True:
            try:
                # ``>`` = "only deliver new messages, never re-deliver pending".
                # Re-delivery of pending entries is the reaper's job
                # (:meth:`claim_idle`) — separating concerns means a
                # crashed handler doesn't tail-spin redelivering its own
                # poison message until it OOMs.
                response = await client.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=batch_size,
                    block=block_ms,
                )
            except asyncio.CancelledError:
                logger.info("eventbus.consume cancelled stream=%s consumer=%s", stream, consumer)
                raise
            except Exception:
                logger.warning(
                    "eventbus.consume read failed stream=%s consumer=%s",
                    stream,
                    consumer,
                    exc_info=True,
                )
                self._client = None
                await asyncio.sleep(1.0)
                client = await self._get_client()
                continue

            if not response:
                continue

            # response shape: [(stream_name, [(id, {field: value, ...}), ...])]
            for _stream_name, entries in response:
                for event_id, fields in entries:
                    await self._handle_one(
                        client=client,
                        stream=stream,
                        group=group,
                        event_id=event_id,
                        fields=fields,
                        handler=handler,
                    )

    async def _handle_one(
        self,
        *,
        client: redis.asyncio.Redis[str],
        stream: str,
        group: str,
        event_id: str,
        fields: dict[str, str],
        handler: EventHandler,
    ) -> None:
        payload = self._decode_payload(fields)
        if payload is None:
            # Unparseable message — ack and drop so it doesn't poison the PEL.
            logger.error(
                "eventbus.consume undecodable event stream=%s id=%s — acking and dropping",
                stream,
                event_id,
            )
            await self._safe_ack(client, stream, group, event_id)
            return

        event = Event(id=event_id, stream=stream, payload=payload)
        try:
            await handler(event)
        except Exception:
            logger.warning(
                "eventbus.consume handler raised stream=%s id=%s — leaving in PEL for redelivery",
                stream,
                event_id,
                exc_info=True,
            )
            return  # do NOT ack; reaper will redeliver

        await self._safe_ack(client, stream, group, event_id)

    @staticmethod
    def _decode_payload(fields: dict[str, str]) -> dict[str, Any] | None:
        raw = fields.get(_PAYLOAD_FIELD)
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict):
            return None
        return decoded

    @staticmethod
    async def _safe_ack(
        client: redis.asyncio.Redis[str],
        stream: str,
        group: str,
        event_id: str,
    ) -> None:
        try:
            await client.xack(stream, group, event_id)  # type: ignore[no-untyped-call]
        except Exception:
            logger.warning(
                "eventbus.consume xack failed stream=%s id=%s — message will be redelivered",
                stream,
                event_id,
                exc_info=True,
            )

    # ── Reaper: redeliver from dead consumers ──────────────────────

    async def claim_idle(
        self,
        *,
        stream: str,
        group: str,
        consumer: str,
        min_idle_ms: int = 300_000,
        handler: EventHandler,
    ) -> int:
        """Steal idle messages from the PEL and re-handle them.

        Uses XAUTOCLAIM, which is the canonical Redis 6.2+ primitive for
        this. Falls back gracefully if the server is older.
        """
        client = await self._get_client()
        handled = 0
        cursor = "0-0"
        try:
            while True:
                # XAUTOCLAIM stream group consumer min_idle start COUNT n
                # Returns: [next_cursor, [(id, {fields...}), ...], deleted_ids]
                result = await client.xautoclaim(
                    name=stream,
                    groupname=group,
                    consumername=consumer,
                    min_idle_time=min_idle_ms,
                    start_id=cursor,
                    count=50,
                )
                # redis-py returns a 3-tuple; older versions return 2-tuple.
                if len(result) == 3:
                    next_cursor, claimed, _deleted = result
                else:
                    next_cursor, claimed = result

                if not claimed:
                    break

                for entry in claimed:
                    if isinstance(entry, tuple) and len(entry) == 2:
                        event_id, fields = entry
                    else:
                        # Defensive — older clients return list-of-lists
                        event_id, fields = entry[0], entry[1]
                    await self._handle_one(
                        client=client,
                        stream=stream,
                        group=group,
                        event_id=event_id,
                        fields=fields,
                        handler=handler,
                    )
                    handled += 1

                cursor = str(next_cursor)
                if cursor == "0-0":
                    break
        except Exception:
            logger.warning(
                "eventbus.claim_idle failed stream=%s group=%s",
                stream,
                group,
                exc_info=True,
            )
        return handled

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


__all__ = ["RedisStreamsEventBus"]
