"""Dispatch-guard webhook self-echo detector (Phase 2, §3.2).

The GitHub App webhook receiver has to decide — cheaply and defensively —
whether an incoming event is a genuine human signal or caretaker's own
output bouncing back through the webhook fabric. A marker regex +
bot-login allowlist gets the common cases right but fails on two edges:

* a real maintainer pastes caretaker's output into a comment so the
  *body* looks like a self-echo even though the actor is human;
* caretaker-authored content routes through a PAT-identified user (not a
  bot login), so an actor-only filter lets it through.

Both miscategorisations waste a CI minute at best and create a
self-trigger loop at worst.

This module encodes both the existing deterministic guard and an LLM
candidate behind the standard :func:`~caretaker.evolution.shadow.shadow_decision`
three-mode switch. The YAML-level JS guard in
``.github/workflows/maintainer.yml`` stays in place as a cheap prefilter
— the workflow can't import Python and the regex saves a CI minute when
the answer is obviously "skip".

Public surface
--------------

* :class:`DispatchVerdict` — pydantic verdict shared by legacy + LLM.
* :class:`DispatchEvent` — a minimal view of a webhook event. Call sites
  construct this from a :class:`~caretaker.github_app.webhooks.ParsedWebhook`.
* :func:`legacy_dispatch_verdict` — the deterministic adapter. Equivalent
  to the existing JS logic. Authoritative when the LLM mode is ``off``
  and the fall-through safety net otherwise.
* :func:`judge_dispatch_llm` — the structured-LLM candidate. Returns
  ``None`` on the cheap-and-unambiguous branches to keep token spend
  bounded.
* :func:`judge_dispatch` — the :func:`shadow_decision`-wrapped entry
  point that call sites should use.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.evolution.shadow import shadow_decision
from caretaker.identity import is_automated

if TYPE_CHECKING:
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────


SuggestedAgent = Literal[
    "pr_agent",
    "issue_agent",
    "review_agent",
    "self_heal_agent",
    "devops_agent",
    "docs_agent",
    "triage",
    "none",
]


class DispatchVerdict(BaseModel):
    """Decision for a single webhook delivery.

    Both legacy and LLM paths produce this shape so the shadow decorator
    can compare them on identical fields.

    Attributes:
        is_self_echo: ``True`` when the event originated from caretaker's
            own output (bot actor + caretaker marker, PAT-routed
            caretaker content, or a comment that the LLM recognised as
            one of caretaker's recent outputs).
        is_human_intent: ``True`` when a human is asking caretaker to do
            something (``@caretaker``/``/caretaker``/``/maintain``
            mention on an otherwise unambiguous body).
        suggested_agent: When ``is_human_intent`` is true, the agent the
            LLM thinks the request is aimed at. Legacy always returns
            ``"none"`` — it has no signal to distinguish agents.
        reason: Short human-readable rationale (capped at 200 chars so
            the shadow record stays log-line sized).
    """

    is_self_echo: bool
    is_human_intent: bool
    suggested_agent: SuggestedAgent = "none"
    reason: str = Field(default="", max_length=200)


# ── Event view ────────────────────────────────────────────────────────────
#
# We don't pass the raw :class:`ParsedWebhook` around because the dispatch
# guard only needs a handful of fields and it is useful in tests to hand
# in a minimal object without faking a full webhook delivery.


@dataclass(frozen=True, slots=True)
class DispatchEvent:
    """Minimal view of a webhook event for dispatch-guard purposes.

    Attributes:
        event_type: ``X-GitHub-Event`` header value (``issue_comment``,
            ``pull_request_review``, ``push``, ``workflow_dispatch``, …).
        actor_login: Login of the user/bot that caused the event. Empty
            string when unknown.
        comment_body: Raw body of the comment/review for events that
            carry one. ``None`` for events without a body (``push``,
            ``workflow_dispatch``, ``check_suite``, …).
        recent_markers: Last few caretaker-authored marker strings on
            the same thread. Forwarded to the LLM so it can spot echoes
            of content caretaker itself produced.
    """

    event_type: str
    actor_login: str = ""
    comment_body: str | None = None
    recent_markers: list[str] = field(default_factory=list)


# ── Legacy adapter ────────────────────────────────────────────────────────


# Matches the JS regex in .github/workflows/maintainer.yml (``<!--\\s*caretaker:[a-z0-9:_-]+``).
# The pattern looks for the ``<!-- caretaker:…`` HTML-comment marker that
# every caretaker-authored comment carries.
_CARETAKER_MARKER_RE = re.compile(r"<!--\s*caretaker:[a-z0-9:_-]+", re.IGNORECASE)

# Lowercase mention tokens that count as explicit human intent to invoke
# caretaker. Keep in sync with the JS guard in ``maintainer.yml``.
_EXPLICIT_TRIGGERS = (
    "@the-care-taker",
    "@caretaker",
    "/caretaker",
    "/maintain",
)

# Events whose payloads carry no meaningful comment body for self-echo
# reasoning — skipping the LLM path for these saves tokens and always
# produces an unambiguous legacy verdict anyway.
_BODILESS_EVENTS: frozenset[str] = frozenset(
    {
        "push",
        "workflow_dispatch",
        "schedule",
        "installation",
        "installation_repositories",
        "ping",
    }
)


def _marker_matches(body: str | None) -> bool:
    if not body:
        return False
    return _CARETAKER_MARKER_RE.search(body) is not None


def _explicit_trigger_matches(body: str | None) -> bool:
    if not body:
        return False
    lower = body.lower()
    return any(token in lower for token in _EXPLICIT_TRIGGERS)


def legacy_dispatch_verdict(event: DispatchEvent) -> DispatchVerdict:
    """Return the deterministic verdict for ``event``.

    This is a faithful port of the JS guard in
    ``.github/workflows/maintainer.yml``:

    * ``is_self_echo`` → ``is_automated(actor) AND marker_regex_matches(body)``.
      The actor-only and marker-only branches individually miss cases
      (PAT-routed caretaker; humans pasting caretaker output); the
      conjunction is deliberately conservative.
    * ``is_human_intent`` → ``NOT is_automated(actor) AND
      explicit_trigger_matches(body)``.
    * ``suggested_agent`` is always ``"none"`` here — the deterministic
      path has no signal to distinguish agents.

    Bodiless events (``push``, ``workflow_dispatch``, …) produce
    ``is_self_echo=False, is_human_intent=False``: the caller is expected
    to route them via the event-type map without a dispatch-guard call.
    """
    actor_automated = is_automated(event.actor_login) if event.actor_login else False
    marker_hit = _marker_matches(event.comment_body)
    trigger_hit = _explicit_trigger_matches(event.comment_body)

    is_self_echo = actor_automated and marker_hit
    is_human_intent = (not actor_automated) and trigger_hit

    if is_self_echo:
        reason = f"legacy: bot actor {event.actor_login!r} + caretaker marker"
    elif is_human_intent:
        reason = f"legacy: human actor {event.actor_login!r} with explicit trigger"
    elif actor_automated:
        reason = f"legacy: bot actor {event.actor_login!r} without marker"
    elif marker_hit:
        reason = "legacy: non-bot actor carrying caretaker marker (ambiguous)"
    else:
        reason = "legacy: no self-echo / no explicit trigger"

    return DispatchVerdict(
        is_self_echo=is_self_echo,
        is_human_intent=is_human_intent,
        suggested_agent="none",
        reason=reason[:200],
    )


# ── LLM candidate ─────────────────────────────────────────────────────────


# Keep the prompt stable-prefixed so the provider's prompt cache can kick
# in. The schema and guidance never change; only the event-specific
# suffix does.
_SYSTEM_PROMPT = (
    "You are caretaker's dispatch guard. Decide whether an incoming GitHub "
    "webhook is (a) caretaker's own output looping back (self-echo), or (b) "
    "a human asking caretaker to do something (human_intent). Humans who "
    "paste caretaker output are NOT self-echoes — distinguish intent from "
    "format. Bot actors emitting plain CI output are not human_intent. "
    "When in doubt about suggested_agent, return 'none'. Keep 'reason' "
    "under 200 chars."
)


def _short_body(body: str | None, *, limit: int = 500) -> str:
    if not body:
        return ""
    if len(body) <= limit:
        return body
    # Tail-end is more informative than head for comments that start with
    # boilerplate banners; keep the most-recent 500 chars.
    return body[-limit:]


def _should_consult_llm(event: DispatchEvent, legacy: DispatchVerdict) -> bool:
    """Return ``True`` when the LLM candidate should actually run.

    Cost-control rules (see Part E of T-A2):

    * Events without a body (``push``, ``workflow_dispatch``, …) are
      skipped — there is no comment body for the LLM to judge.
    * Legacy-unambiguous self-echoes (bot actor + marker, body absent)
      and legacy-unambiguous noops (no marker, no trigger) are skipped —
      the deterministic path already produces the right answer and we
      save the tokens.
    * Every other combination is *ambiguous* from the deterministic
      path's perspective (PAT-routed caretaker, humans pasting caretaker
      output, weird mention spellings) and worth the tokens.
    """
    if event.event_type in _BODILESS_EVENTS:
        return False
    if event.comment_body is None:
        return False

    actor_automated = is_automated(event.actor_login) if event.actor_login else False
    marker_hit = _marker_matches(event.comment_body)
    trigger_hit = _explicit_trigger_matches(event.comment_body)

    # Bot actor + no marker + no trigger → legacy already says "skip, no
    # self-echo"; nothing for the LLM to disambiguate.
    if actor_automated and not marker_hit and not trigger_hit:
        return False
    # No marker + no trigger + non-bot actor → plain comment, legacy
    # already says "no self-echo, no explicit trigger"; save the tokens.
    if (not actor_automated) and not marker_hit and not trigger_hit:
        return False

    # Everything else — humans pasting caretaker output, PAT-routed
    # caretaker, bot reviews with mentions, etc. — is genuinely
    # ambiguous; spend the tokens.
    _ = legacy  # reserved for future per-branch policies
    return True


async def judge_dispatch_llm(
    event: DispatchEvent,
    *,
    claude: ClaudeClient | None,
) -> DispatchVerdict | None:
    """LLM candidate for :func:`shadow_decision`.

    Returns a :class:`DispatchVerdict` when the model is consulted, or
    ``None`` on the cheap-and-unambiguous short-circuit path. The
    shadow-wrap treats ``None`` as "keep legacy authoritative", which
    exactly matches the cost-control policy we want: only pay tokens
    when the deterministic path is ambiguous.

    Errors (provider outage, schema validation, JSON decode) are
    swallowed and returned as ``None``. The shadow decorator's ``enforce``
    branch falls through to legacy on ``None`` too, so callers never see
    a half-broken candidate bubble up.
    """
    if claude is None or not getattr(claude, "available", False):
        return None

    # Re-run the legacy check here so the short-circuit is self-contained
    # and the candidate can be invoked in isolation (tests, retries).
    legacy = legacy_dispatch_verdict(event)
    if not _should_consult_llm(event, legacy):
        return None

    prompt = _build_prompt(event)
    try:
        verdict = await claude.structured_complete(
            prompt,
            schema=DispatchVerdict,
            feature="dispatch_guard",
            system=_SYSTEM_PROMPT,
        )
    except Exception as exc:  # provider outage, bad JSON, schema mismatch
        logger.warning(
            "dispatch_guard LLM candidate failed (event=%s actor=%s): %s: %s",
            event.event_type,
            event.actor_login,
            type(exc).__name__,
            exc,
        )
        return None

    # Clamp the reason field defensively — ``structured_complete`` already
    # validates ``max_length=200`` but a forgiving model may slip a
    # longer string past if we ever relax the schema.
    if len(verdict.reason) > 200:
        verdict = verdict.model_copy(update={"reason": verdict.reason[:200]})
    return verdict


def _build_prompt(event: DispatchEvent) -> str:
    """Serialise the variable suffix of the LLM prompt.

    Kept separate from :data:`_SYSTEM_PROMPT` so call-site-invariant text
    lands in the stable system block (cache-friendly) while the
    per-event payload lives in the user turn.
    """
    body_snippet = _short_body(event.comment_body)
    markers = "\n".join(f"  - {m}" for m in event.recent_markers[:5]) or "  (none)"
    return (
        f"event_type: {event.event_type}\n"
        f"actor_login: {event.actor_login or '(unknown)'}\n"
        f"recent_caretaker_markers:\n{markers}\n"
        f"comment_body (last 500 chars):\n{body_snippet}"
    )


# ── Shadow-wrapped entry point ────────────────────────────────────────────


def _dispatch_compare(a: DispatchVerdict | None, b: DispatchVerdict | None) -> bool:
    """Treat two verdicts as equal when the dispatch-affecting fields match.

    The ``reason`` string is advisory and legacy/LLM phrase it differently
    even when they agree on the outcome; comparing on it would flood the
    disagreement stream with noise.  ``suggested_agent`` is also excluded
    because legacy can never produce anything but ``"none"``.

    The candidate may return ``None`` on purpose (cost-control
    short-circuit) — treat that as "no signal, no disagreement" rather
    than a raw inequality. The shadow decorator tags those records as
    ``agree`` which avoids filling the disagreement stream with
    expected short-circuits.
    """
    if a is None or b is None:
        return True
    return (a.is_self_echo, a.is_human_intent) == (b.is_self_echo, b.is_human_intent)


@shadow_decision("dispatch_guard", compare=_dispatch_compare)
async def judge_dispatch(
    event: DispatchEvent,
    *,
    claude: ClaudeClient | None = None,
    legacy: object = None,  # supplied by the decorator
    candidate: object = None,  # supplied by the decorator
    context: dict[str, object] | None = None,  # supplied by the decorator
) -> DispatchVerdict:
    """Public dispatch-guard decision site.

    Callers invoke this directly and the :func:`shadow_decision`
    decorator takes care of picking legacy / candidate based on
    ``config.agentic.dispatch_guard.mode``. The two implementations are
    supplied via the ``legacy``/``candidate`` keyword arguments so the
    decorator stays importless.
    """
    # The body of this function is only reached on decorator
    # misconfiguration — the wrapper always returns before dispatching
    # here. Raise a descriptive TypeError to surface programmer errors
    # early rather than silently shadowing.
    raise TypeError(  # pragma: no cover — decorator handles dispatch
        "judge_dispatch must be invoked via its shadow_decision wrapper; "
        "pass legacy=..., candidate=..., context=... keyword arguments."
    )


async def evaluate_dispatch(
    event: DispatchEvent,
    *,
    claude: ClaudeClient | None = None,
    context: dict[str, object] | None = None,
) -> DispatchVerdict:
    """Convenience wrapper that wires legacy + candidate into :func:`judge_dispatch`.

    Call sites that already have a :class:`ClaudeClient` available should
    prefer this — it hides the ``legacy=``/``candidate=`` kwargs that the
    shadow decorator needs.

    ``legacy`` and ``candidate`` accept the same ``(event, *, claude=...)``
    signature :func:`judge_dispatch` itself exposes so the shadow
    decorator can forward positional + keyword args through unchanged.
    """

    async def _legacy(ev: DispatchEvent, *, claude: ClaudeClient | None = None) -> DispatchVerdict:
        _ = claude  # legacy path is deterministic; no LLM needed
        return legacy_dispatch_verdict(ev)

    async def _candidate(
        ev: DispatchEvent, *, claude: ClaudeClient | None = None
    ) -> DispatchVerdict | None:
        return await judge_dispatch_llm(ev, claude=claude)

    return await judge_dispatch(
        event,
        claude=claude,
        legacy=_legacy,
        candidate=_candidate,
        context=context,
    )


__all__ = [
    "DispatchEvent",
    "DispatchVerdict",
    "SuggestedAgent",
    "evaluate_dispatch",
    "judge_dispatch",
    "judge_dispatch_llm",
    "legacy_dispatch_verdict",
]
