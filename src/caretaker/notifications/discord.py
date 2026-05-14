"""Discord notification sink.

Sends rich embed messages to a Discord channel via the Bot REST API.
Designed to be fire-and-forget from agent hot paths — errors are logged
but never propagated so they cannot break the primary GitHub write path.

Usage::

    notifier = DiscordNotifier.from_config(cfg.discord)
    if notifier:
        await notifier.send_embed(
            title="PR review complete",
            description=full_review_text,
            color=DiscordColor.SUCCESS,
        )
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import DiscordConfig

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"


class DiscordColor:
    SUCCESS = 0x22C55E  # green
    FAILURE = 0xEF4444  # red
    WARNING = 0xF59E0B  # amber
    INFO = 0x3B82F6  # blue
    NEUTRAL = 0x6B7280  # gray


class DiscordNotifier:
    """Thin async wrapper around the Discord messages REST endpoint."""

    def __init__(self, *, bot_token: str, channel_id: str) -> None:
        self._token = bot_token
        self._channel_id = channel_id

    @classmethod
    def from_config(cls, cfg: DiscordConfig) -> DiscordNotifier | None:
        """Return a notifier if Discord is enabled and credentials are present."""
        if not cfg.enabled:
            return None
        token = os.environ.get(cfg.bot_token_env, "").strip()
        if not token:
            logger.warning(
                "discord: enabled but %s is not set; notifications disabled",
                cfg.bot_token_env,
            )
            return None
        if not cfg.channel_id:
            logger.warning("discord: enabled but channel_id is empty; notifications disabled")
            return None
        return cls(bot_token=token, channel_id=cfg.channel_id)

    async def send_embed(
        self,
        *,
        title: str,
        description: str,
        color: int = DiscordColor.INFO,
        fields: list[dict[str, object]] | None = None,
        url: str | None = None,
        footer: str | None = None,
    ) -> None:
        """Post a rich embed card to the configured channel.

        Never raises — errors are logged at WARNING level.
        Description is truncated to Discord's 4096-char embed limit.
        """
        embed: dict[str, object] = {
            "title": title[:256],
            "description": description[:4096],
            "color": color,
        }
        if url:
            embed["url"] = url
        if fields:
            embed["fields"] = [
                {
                    "name": str(f.get("name", ""))[:256],
                    "value": str(f.get("value", ""))[:1024],
                    "inline": bool(f.get("inline", False)),
                }
                for f in fields[:25]
            ]
        if footer:
            embed["footer"] = {"text": footer[:2048]}

        await self._post({"embeds": [embed]})

    async def send(self, content: str) -> None:
        """Post a plain text message (≤ 2000 chars)."""
        await self._post({"content": content[:2000]})

    async def _post(self, payload: dict[str, object]) -> None:
        try:
            import httpx
        except ImportError:
            logger.warning("discord: httpx not available; cannot send notification")
            return

        url = f"{_DISCORD_API}/channels/{self._channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                body = resp.text[:200]
                logger.warning("discord: POST failed status=%d body=%s", resp.status_code, body)
        except Exception as exc:
            logger.warning("discord: send failed: %s", exc)


__all__ = ["DiscordColor", "DiscordNotifier"]
