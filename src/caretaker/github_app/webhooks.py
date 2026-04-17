"""GitHub webhook helpers: signature verification and payload parsing.

All webhook deliveries carry an ``X-Hub-Signature-256`` header that is an
HMAC-SHA256 of the raw request body, keyed on the App's webhook secret.
We validate in constant time and treat any mismatch or missing header as
an authentication failure.

We also provide a tiny ``parse_webhook`` helper that extracts the fields
caretaker actually needs (event type, delivery id, installation id, and
the JSON body).  The full event schema lives on GitHub's side — we keep
the parser small and type-strict so the rest of caretaker is insulated
from schema churn.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Hub-Signature-256"
_DELIVERY_HEADER = "X-GitHub-Delivery"
_EVENT_HEADER = "X-GitHub-Event"


class WebhookSignatureError(Exception):
    """Raised when a webhook fails signature verification."""


@dataclass(frozen=True, slots=True)
class ParsedWebhook:
    """Minimally-parsed webhook — just what the dispatcher needs."""

    event_type: str
    delivery_id: str
    action: str | None
    installation_id: int | None
    repository_full_name: str | None
    payload: dict[str, Any]


def verify_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: str | None,
) -> None:
    """Constant-time verify a ``sha256=...`` GitHub webhook signature.

    Raises
    ------
    WebhookSignatureError:
        If ``signature_header`` is missing, malformed, or does not match
        the HMAC computed over ``body`` using ``secret``.
    """
    if not secret:
        raise WebhookSignatureError("webhook secret is not configured")
    if not signature_header:
        raise WebhookSignatureError(f"missing {_SIGNATURE_HEADER} header")
    if not signature_header.startswith("sha256="):
        raise WebhookSignatureError(f"unexpected signature scheme: {signature_header!r}")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1].strip()
    if not hmac.compare_digest(expected, provided):
        raise WebhookSignatureError("signature mismatch")


def parse_webhook(
    *,
    body: bytes,
    headers: dict[str, str],
) -> ParsedWebhook:
    """Parse a verified webhook body into a :class:`ParsedWebhook`.

    Header lookups are case-insensitive; callers can pass the raw
    FastAPI / Starlette headers dict.
    """
    event_type = _case_insensitive_get(headers, _EVENT_HEADER)
    delivery_id = _case_insensitive_get(headers, _DELIVERY_HEADER)
    if not event_type:
        raise ValueError(f"missing {_EVENT_HEADER} header")
    if not delivery_id:
        raise ValueError(f"missing {_DELIVERY_HEADER} header")

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"webhook body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("webhook payload root must be an object")

    action = payload.get("action")
    if action is not None and not isinstance(action, str):
        action = None

    installation_id: int | None = None
    installation = payload.get("installation")
    if isinstance(installation, dict):
        raw = installation.get("id")
        if isinstance(raw, int) and raw > 0:
            installation_id = raw

    repo_full_name: str | None = None
    repo = payload.get("repository")
    if isinstance(repo, dict):
        raw_name = repo.get("full_name")
        if isinstance(raw_name, str):
            repo_full_name = raw_name

    return ParsedWebhook(
        event_type=event_type,
        delivery_id=delivery_id,
        action=action,
        installation_id=installation_id,
        repository_full_name=repo_full_name,
        payload=payload,
    )


def _case_insensitive_get(headers: dict[str, str], key: str) -> str | None:
    target = key.lower()
    for name, value in headers.items():
        if name.lower() == target:
            return value
    return None


__all__ = [
    "ParsedWebhook",
    "WebhookSignatureError",
    "parse_webhook",
    "verify_signature",
]
