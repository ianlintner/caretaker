"""Admin webhook delivery log endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from caretaker.admin.auth import UserInfo, require_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Module-level delivery log — in-memory ring buffer
# Populated by the webhook handler via register_delivery()
_deliveries: list[dict[str, Any]] = []
_MAX_DELIVERIES = 500


def register_delivery(
    *,
    event: str,
    action: str | None,
    installation_id: int | None,
    delivery_id: str | None,
    received_at: str,
    agents_fired: list[str],
    status: str = "ok",
) -> None:
    """Called from the webhook handler on each delivery. Thread-safe (GIL)."""
    global _deliveries  # noqa: PLW0603
    record: dict[str, Any] = {
        "event": event,
        "action": action,
        "installation_id": installation_id,
        "delivery_id": delivery_id,
        "received_at": received_at,
        "agents_fired": agents_fired,
        "status": status,
    }
    _deliveries.append(record)
    if len(_deliveries) > _MAX_DELIVERIES:
        _deliveries = _deliveries[-_MAX_DELIVERIES:]


def reset_for_tests() -> None:
    global _deliveries  # noqa: PLW0603
    _deliveries = []


@router.get("/webhooks/deliveries")
async def list_webhook_deliveries(
    limit: int = Query(default=100, ge=1, le=500),
    event: str | None = Query(default=None, description="Filter by event type"),
    _user: UserInfo = Depends(require_session),
) -> dict[str, Any]:
    """Return recent webhook deliveries (newest first)."""
    rows = list(reversed(_deliveries))
    if event:
        rows = [r for r in rows if r.get("event") == event]
    return {"items": rows[:limit], "total": len(rows)}


__all__ = ["register_delivery", "reset_for_tests", "router"]
