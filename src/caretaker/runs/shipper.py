"""Runner-side log shipper: ``caretaker stream``.

The shipper is the runtime side of the streaming-run architecture. It
turns a thin GitHub Actions job into a triggering tail:

1. Mint an OIDC JWT bound to the backend's audience.
2. ``POST /runs/start`` with the OIDC token → backend returns
   ``run_id``, ``ingest_token``, and the per-run endpoints.
3. ``POST /runs/{id}/trigger`` to ask the backend to execute caretaker.
4. Open the SSE stream and tee events to stdout so the GitHub Actions
   log shows the live output. Exit when the backend emits ``event:end``,
   propagating its exit code.

The shipper never runs caretaker locally and never touches the GitHub
API directly — the backend does both. This eliminates the need for any
secret in consumer repos beyond the bare ``CARETAKER_BACKEND_URL``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from caretaker.runs.oidc_minter import OIDCMintError, mint_actions_oidc_token

logger = logging.getLogger(__name__)


_DEFAULT_AUDIENCE = "caretaker-backend"
_RETRY_INITIAL_DELAY = 0.2
_RETRY_MAX_DELAY = 30.0
_RETRY_TOTAL_BUDGET = 5 * 60.0


@dataclass(frozen=True)
class StreamConfig:
    backend_url: str
    audience: str
    mode: str
    event_type: str | None
    event_payload: dict[str, Any]
    tail: bool
    timeout: float = 30.0


def _config_from_env(
    *,
    mode: str,
    tail: bool,
    event_type: str | None,
    event_payload: dict[str, Any] | None,
) -> StreamConfig:
    backend = os.environ.get("CARETAKER_BACKEND_URL", "").rstrip("/")
    if not backend:
        raise RuntimeError("CARETAKER_BACKEND_URL is required (e.g. https://caretaker.example.com)")
    audience = os.environ.get("CARETAKER_OIDC_AUDIENCE") or _DEFAULT_AUDIENCE
    return StreamConfig(
        backend_url=backend,
        audience=audience,
        mode=mode,
        event_type=event_type,
        event_payload=event_payload or {},
        tail=tail,
    )


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


async def _retry_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    json_body: Any | None = None,
    content: bytes | None = None,
    content_type: str | None = None,
) -> httpx.Response:
    delay = _RETRY_INITIAL_DELAY
    elapsed = 0.0
    last_exc: Exception | None = None
    while elapsed < _RETRY_TOTAL_BUDGET:
        try:
            req_headers = dict(headers)
            if content is not None and content_type:
                req_headers["Content-Type"] = content_type
                resp = await client.post(url, headers=req_headers, content=content)
            else:
                resp = await client.post(url, headers=req_headers, json=json_body)
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last_exc = RuntimeError(f"server error {resp.status_code}: {resp.text[:200]}")
        except (TimeoutError, httpx.HTTPError) as exc:
            last_exc = exc
        await asyncio.sleep(delay)
        elapsed += delay
        delay = min(delay * 2.0, _RETRY_MAX_DELAY)
    raise RuntimeError(f"retry budget exhausted for POST {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


async def _start_run(
    client: httpx.AsyncClient,
    cfg: StreamConfig,
    oidc_jwt: str,
) -> dict[str, Any]:
    body = {
        "mode": cfg.mode,
        "event_type": cfg.event_type,
        "config_digest": "",
        "caretaker_version": "",
    }
    resp = await _retry_post(
        client,
        f"{cfg.backend_url}/runs/start",
        headers={
            "Authorization": f"Bearer {oidc_jwt}",
            "Accept": "application/json",
        },
        json_body=body,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"/runs/start failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def _trigger_run(
    client: httpx.AsyncClient,
    *,
    trigger_url: str,
    ingest_token: str,
    cfg: StreamConfig,
) -> dict[str, Any]:
    body = {
        "mode": cfg.mode,
        "event_type": cfg.event_type,
        "event_payload": cfg.event_payload,
    }
    resp = await _retry_post(
        client,
        trigger_url,
        headers={
            "Authorization": f"Bearer {ingest_token}",
            "Accept": "application/json",
        },
        json_body=body,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"/runs/.../trigger failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def _tail(
    cfg: StreamConfig,
    *,
    stream_url: str,
    ingest_token: str,
) -> int:
    """Tail the SSE stream; print each log line to stdout. Return exit code."""
    headers = {
        "Authorization": f"Bearer {ingest_token}",
        "Accept": "text/event-stream",
    }
    last_event_id: str | None = None
    exit_code = 0
    backoff = 1.0
    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            req_headers = dict(headers)
            if last_event_id is not None:
                req_headers["Last-Event-ID"] = last_event_id
            try:
                async with client.stream("GET", stream_url, headers=req_headers) as resp:
                    if resp.status_code != 200:
                        text = await resp.aread()
                        raise RuntimeError(f"SSE connect failed: {resp.status_code} {text[:200]!r}")
                    backoff = 1.0
                    event_name = "message"
                    data_lines: list[str] = []
                    event_id: str | None = None
                    async for raw in resp.aiter_lines():
                        if not raw:
                            # Dispatch the buffered event.
                            data = "\n".join(data_lines)
                            if data or event_name != "message":
                                done = _print_event(event_name, data)
                                if event_id is not None:
                                    last_event_id = event_id
                                if done is not None:
                                    exit_code = done
                                    return exit_code
                            event_name = "message"
                            data_lines = []
                            event_id = None
                            continue
                        if raw.startswith(":"):
                            continue  # comment / heartbeat
                        if raw.startswith("event:"):
                            event_name = raw.split(":", 1)[1].strip()
                        elif raw.startswith("data:"):
                            data_lines.append(raw.split(":", 1)[1].lstrip())
                        elif raw.startswith("id:"):
                            event_id = raw.split(":", 1)[1].strip()
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("SSE tail error: %s; reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)


def _print_event(event_name: str, data: str) -> int | None:
    """Render an SSE event to the runner's stdout. Return exit code if terminal."""
    if event_name == "log":
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            print(data, flush=True)
            return None
        stream = obj.get("stream", "stdout")
        msg = obj.get("data", "")
        if stream == "stderr" or stream == "system":
            print(msg, file=sys.stderr, flush=True)
        else:
            print(msg, flush=True)
        return None
    if event_name == "end":
        # `data` is the run status string ("succeeded", "failed", "stalled", …).
        status = data.strip().lower()
        return 0 if status == "succeeded" else 1
    if event_name == "gap":
        print(f"[caretaker] missed events: {data}", file=sys.stderr, flush=True)
        return None
    return None


async def run(
    *,
    mode: str = "full",
    tail: bool = True,
    event_type: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> int:
    """Top-level shipper entrypoint. Returns process exit code."""
    cfg = _config_from_env(
        mode=mode,
        tail=tail,
        event_type=event_type,
        event_payload=event_payload,
    )

    try:
        oidc_jwt = await mint_actions_oidc_token(audience=cfg.audience)
    except OIDCMintError as exc:
        print(f"[caretaker] OIDC mint failed: {exc}", file=sys.stderr, flush=True)
        return 2

    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        try:
            start = await _start_run(client, cfg, oidc_jwt)
        except RuntimeError as exc:
            print(f"[caretaker] start failed: {exc}", file=sys.stderr, flush=True)
            return 2

        run_id = start["run_id"]
        ingest_token = start["ingest_token"]
        trigger_url = start["trigger_endpoint"]
        stream_url = start["stream_url"]
        finish_url = start["finish_endpoint"]

        print(
            f"[caretaker] run_id={run_id} mode={cfg.mode} backend={cfg.backend_url}",
            flush=True,
        )

        try:
            await _trigger_run(
                client,
                trigger_url=trigger_url,
                ingest_token=ingest_token,
                cfg=cfg,
            )
        except RuntimeError as exc:
            print(f"[caretaker] trigger failed: {exc}", file=sys.stderr, flush=True)
            await _post_finish(
                client, finish_url, ingest_token, exit_code=2, summary={"error": str(exc)[:300]}
            )
            return 2

    if not cfg.tail:
        return 0

    return await _tail(cfg, stream_url=stream_url, ingest_token=ingest_token)


async def _post_finish(
    client: httpx.AsyncClient,
    finish_url: str,
    ingest_token: str,
    *,
    exit_code: int,
    summary: dict[str, Any] | None = None,
) -> None:
    body = {"exit_code": exit_code, "summary": summary or {}, "report_json": None}
    try:
        await client.post(
            finish_url,
            headers={
                "Authorization": f"Bearer {ingest_token}",
                "Accept": "application/json",
            },
            json=body,
        )
    except httpx.HTTPError as exc:
        logger.warning("finish post failed: %s", exc)


__all__ = ["StreamConfig", "run"]
