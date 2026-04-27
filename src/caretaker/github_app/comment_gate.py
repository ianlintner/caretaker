"""Pre-dispatch gate for comment-carrying webhook events.

Today the :class:`~caretaker.github_app.dispatcher.WebhookDispatcher`
resolves agents purely from the event type. That works for state-driven
events (PR opened, CI finished, push) but it has two failure modes for
comment-driven events:

1. **Self-echo loops.** When caretaker posts a comment, GitHub fires an
   ``issue_comment.created`` webhook back at us. Without a guard, that
   bounce dispatches ``["issue", "pr"]`` agents which may post another
   comment, ad infinitum. Today the agents themselves are expected to
   no-op on caretaker's own marker — but the dispatcher has no defence
   in depth.

2. **Silent ``@caretaker`` requests.** A user types ``@caretaker take
   this over`` and gets no feedback. Today the comment is delivered to
   the same ``["issue", "pr"]`` agents that fire on every PR comment,
   so there's no signal in metrics or logs distinguishing "human asked
   for caretaker" from generic comment traffic. Operators can't tell
   whether the trigger was even received.

This module solves both with a tiny, deterministic gate that runs *in
front of* the dispatcher's agent resolution. It re-uses
:func:`~caretaker.github_app.dispatch_guard.legacy_dispatch_verdict` so
the trigger-string set stays in one place.

Behaviour is controlled by :envvar:`CARETAKER_WEBHOOK_COMMENT_GATING`:

* ``off`` — no gating; matches behaviour prior to this module.
* ``advise`` (default) — self-echo events are skipped; human-intent
  events are tagged in logs + metrics (``outcome="human_intent"``) and
  then proceed to normal agent dispatch. Other comment events behave as
  before.
* ``enforce`` — self-echo events are skipped; comment events that
  carry no explicit human intent are also skipped. Only human-intent
  or non-comment events run agents.

Skips are *not* errors — they record ``outcome="self_echo"`` /
``outcome="no_human_intent"`` and return success ack to GitHub.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from caretaker.github_app.dispatch_guard import (
    DispatchEvent,
    DispatchVerdict,
    legacy_dispatch_verdict,
)

if TYPE_CHECKING:
    from caretaker.github_app.webhooks import ParsedWebhook

logger = logging.getLogger(__name__)


# Webhook event types whose payload carries a comment body that should
# be inspected for trigger strings / self-echo markers. Other event
# types (``pull_request``, ``push``, ``check_run``, …) are state-driven
# and never gated — their agents fire unconditionally.
_COMMENT_EVENTS: frozenset[str] = frozenset(
    {
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
    }
)


class CommentGateMode(StrEnum):
    """Operational mode for the comment gate.

    Values match the ``CARETAKER_WEBHOOK_COMMENT_GATING`` env var.
    """

    OFF = "off"
    ADVISE = "advise"
    ENFORCE = "enforce"

    @classmethod
    def parse(cls, raw: str | None) -> CommentGateMode:
        """Parse ``raw`` into a mode, defaulting to :attr:`ADVISE`.

        Unknown values resolve to :attr:`ADVISE` — the gate's safe
        default skips self-echo loops without changing agent dispatch
        for non-self-echo events. Operators set ``off`` to revert
        entirely or ``enforce`` to require explicit triggers.
        """
        if not raw:
            return cls.ADVISE
        try:
            return cls(raw.strip().lower())
        except ValueError:
            logger.warning(
                "Unknown CARETAKER_WEBHOOK_COMMENT_GATING %r; defaulting to 'advise'",
                raw,
            )
            return cls.ADVISE


@dataclass(frozen=True, slots=True)
class CommentGateDecision:
    """Outcome of evaluating the gate for a single webhook delivery.

    Attributes:
        skip: ``True`` when the dispatcher should short-circuit and not
            resolve agents. The caller still records a metric and acks
            the delivery.
        outcome: Bounded label suitable for the
            ``caretaker_webhook_events_total{outcome=...}`` metric.
            One of ``"self_echo"``, ``"no_human_intent"``,
            ``"human_intent"``, or ``"proceed"``.
        verdict: The underlying :class:`DispatchVerdict` (or ``None``
            when the event was not comment-gated, e.g. ``push``).
        reason: Short human-readable rationale, mirroring
            ``verdict.reason`` when present. Empty string for plain
            proceed paths.
    """

    skip: bool
    outcome: str
    verdict: DispatchVerdict | None
    reason: str


_PROCEED = CommentGateDecision(
    skip=False,
    outcome="proceed",
    verdict=None,
    reason="",
)


def _extract_actor_and_body(parsed: ParsedWebhook) -> tuple[str, str | None]:
    """Pull ``(actor_login, comment_body)`` out of a comment-event payload.

    The comment object lives under different keys per event:

    * ``issue_comment`` / ``pull_request_review_comment`` →
      ``payload["comment"]``
    * ``pull_request_review`` → ``payload["review"]``

    Both shapes carry ``{"body": str, "user": {"login": str}}``. We fall
    back to the top-level ``"sender"`` for the actor when the nested
    user is missing (which only happens on synthetic test payloads).
    Empty strings are returned for missing fields — callers treat
    those as "no signal", matching :func:`legacy_dispatch_verdict`.
    """
    payload = parsed.payload
    actor = ""
    body: str | None = None

    obj = payload.get("comment") or payload.get("review")
    if isinstance(obj, dict):
        raw_body = obj.get("body")
        if isinstance(raw_body, str):
            body = raw_body
        user = obj.get("user")
        if isinstance(user, dict):
            login = user.get("login")
            if isinstance(login, str):
                actor = login

    if not actor:
        sender = payload.get("sender")
        if isinstance(sender, dict):
            login = sender.get("login")
            if isinstance(login, str):
                actor = login

    return actor, body


def evaluate_comment_gate(
    parsed: ParsedWebhook,
    *,
    mode: CommentGateMode | None = None,
) -> CommentGateDecision:
    """Decide whether a parsed webhook should reach the agent dispatcher.

    Returns :data:`_PROCEED` (``skip=False, outcome="proceed"``) for any
    event that is not comment-gated, or when the gate is ``off``. Other
    decisions:

    * Self-echo (any mode except ``off``) →
      ``skip=True, outcome="self_echo"``.
    * Human intent (``advise`` or ``enforce``) →
      ``skip=False, outcome="human_intent"`` (still runs agents, but
      surfaces the trigger to logs/metrics).
    * No-human-intent on a comment event in ``enforce`` mode →
      ``skip=True, outcome="no_human_intent"``.
    * No-human-intent on a comment event in ``advise`` mode →
      ``skip=False, outcome="proceed"`` (no behavior change).

    The optional ``mode`` argument is for tests; production reads the
    env var via :meth:`CommentGateMode.parse`.
    """
    resolved_mode = mode or CommentGateMode.parse(
        os.environ.get("CARETAKER_WEBHOOK_COMMENT_GATING")
    )
    if resolved_mode is CommentGateMode.OFF:
        return _PROCEED
    if parsed.event_type not in _COMMENT_EVENTS:
        return _PROCEED

    actor, body = _extract_actor_and_body(parsed)
    verdict = legacy_dispatch_verdict(
        DispatchEvent(
            event_type=parsed.event_type,
            actor_login=actor,
            comment_body=body,
        )
    )

    if verdict.is_self_echo:
        return CommentGateDecision(
            skip=True,
            outcome="self_echo",
            verdict=verdict,
            reason=verdict.reason,
        )

    if verdict.is_human_intent:
        return CommentGateDecision(
            skip=False,
            outcome="human_intent",
            verdict=verdict,
            reason=verdict.reason,
        )

    if resolved_mode is CommentGateMode.ENFORCE:
        return CommentGateDecision(
            skip=True,
            outcome="no_human_intent",
            verdict=verdict,
            reason=verdict.reason,
        )

    return _PROCEED


__all__ = [
    "CommentGateDecision",
    "CommentGateMode",
    "evaluate_comment_gate",
]
