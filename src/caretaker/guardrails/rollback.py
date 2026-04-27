"""Checkpoint-and-rollback wrapper for post-merge state mutations.

Agentic Design Patterns Ch. 18 calls this pattern "Checkpoint & Rollback":
a destructive action (here: a GitHub merge) is committed, then the agent
watches a verification signal for a bounded window and fires a compensating
rollback if the verification fails inside that window.

For caretaker's merge path the verification signal is **base-branch CI**.
When a PR merges into ``main`` we poll the latest check suite on the base
branch; if any required check flips to ``failure`` within the window, we
call the caller-supplied ``rollback`` (typically a `git revert` + issue
open) and emit a ``caretaker_guardrail_rollback_fired_total`` metric
increment. On green CI or a timeout without a red signal we exit cleanly.

Usage
-----

.. code-block:: python

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="owner/name", label="merge#123"),
        verify=verify_base_ci,
        rollback=revert_merge,
        window_seconds=300,
        poll_interval_seconds=15,
    )
    # result.outcome is one of: ``verified`` / ``rolled_back`` / ``rollback_failed``

The wrapper is deliberately sync-surface for the action: the caller has
already performed the merge by the time they hand us a ``CheckpointedAction``.
``verify`` and ``rollback`` are both async callables — the wrapper owns the
polling loop, not the caller.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from caretaker.observability.metrics import record_guardrail_rollback_fired

logger = logging.getLogger(__name__)


# ── Types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CheckpointedAction:
    """Identifies the action being guarded so metrics + logs can correlate.

    Attributes
    ----------
    repo
        GitHub ``owner/name`` slug (or any stable repo identifier). Used
        as a metric label; bounded cardinality is the caller's concern
        (the metric label is cardinality-conscious — operators should
        keep the slug stable across runs).
    label
        Short human-readable identifier for the specific action (e.g.
        ``"merge#123"``). Logged but not used as a metric label.
    """

    repo: str
    label: str = ""


class RollbackOutcome(StrEnum):
    """Closed enum describing how a checkpoint window ended."""

    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Structured result of one :func:`checkpoint_and_rollback` call."""

    outcome: RollbackOutcome
    attempts: int
    reason: str = ""


# ── Verify / rollback protocol ──────────────────────────────────────────

# ``verify`` returns one of:
#   - True  → signal is healthy; wrapper keeps polling until window ends
#   - False → signal is red; wrapper triggers rollback and exits
#   - None  → signal is still pending; wrapper keeps polling
#
# Encoding the three-way state as ``bool | None`` keeps the caller API
# minimal and lets us distinguish "verification in progress" from
# "verification passed".
VerifyResult = bool | None
VerifyFn = Callable[[], Awaitable[VerifyResult]]
RollbackFn = Callable[[], Awaitable[None]]


# ── Public API ──────────────────────────────────────────────────────────


async def checkpoint_and_rollback(
    action: CheckpointedAction,
    verify: VerifyFn,
    rollback: RollbackFn,
    *,
    window_seconds: int = 300,
    poll_interval_seconds: int = 15,
    enabled: bool = True,
) -> CheckpointResult:
    """Poll ``verify`` for ``window_seconds``; fire ``rollback`` on failure.

    Parameters
    ----------
    action
        Identifies the guarded action for logs and metrics.
    verify
        Async callable returning ``True`` (healthy), ``False`` (failed),
        or ``None`` (still pending). Called every
        ``poll_interval_seconds`` until the window closes or we get a
        decisive verdict.
    rollback
        Async callable to run when ``verify`` returns ``False``. Should
        be idempotent — failed rollbacks are logged but not retried.
    window_seconds
        Upper bound on the total time we wait for a verdict.
    poll_interval_seconds
        Delay between consecutive ``verify`` calls.
    enabled
        When ``False``, the wrapper returns :attr:`RollbackOutcome.SKIPPED`
        immediately without calling ``verify`` or ``rollback``. Lets
        callers wire this in unconditionally while the operator flag
        (``pr_agent.merge_rollback.enabled``) is still off.
    """
    if not enabled:
        logger.debug(
            "guardrails.checkpoint_and_rollback: disabled for action=%s label=%s",
            action.repo,
            action.label,
        )
        return CheckpointResult(
            outcome=RollbackOutcome.SKIPPED,
            attempts=0,
            reason="disabled",
        )

    deadline = asyncio.get_event_loop().time() + max(0, window_seconds)
    attempts = 0
    last_verdict: VerifyResult = None
    while True:
        attempts += 1
        try:
            last_verdict = await verify()
        except Exception as exc:  # noqa: BLE001 — verify must not crash the wrapper
            logger.warning(
                "guardrails.checkpoint_and_rollback: verify raised for %s (%s): %s",
                action.label or action.repo,
                type(exc).__name__,
                exc,
            )
            last_verdict = None

        if last_verdict is True:
            logger.info(
                "guardrails.checkpoint_and_rollback: verified clean for %s after %d attempt(s)",
                action.label or action.repo,
                attempts,
            )
            return CheckpointResult(outcome=RollbackOutcome.VERIFIED, attempts=attempts)

        if last_verdict is False:
            logger.warning(
                "guardrails.checkpoint_and_rollback: verify returned False for %s — rolling back",
                action.label or action.repo,
            )
            try:
                await rollback()
            except Exception as exc:  # noqa: BLE001 — rollback failure must surface
                logger.error(
                    "guardrails.checkpoint_and_rollback: rollback FAILED for %s: %s: %s",
                    action.label or action.repo,
                    type(exc).__name__,
                    exc,
                )
                record_guardrail_rollback_fired(repo=action.repo, reason="rollback_failed")
                return CheckpointResult(
                    outcome=RollbackOutcome.ROLLBACK_FAILED,
                    attempts=attempts,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            record_guardrail_rollback_fired(repo=action.repo, reason="verify_failed")
            return CheckpointResult(
                outcome=RollbackOutcome.ROLLED_BACK,
                attempts=attempts,
                reason="verify_failed",
            )

        # last_verdict is None — still pending. Check the window.
        now = asyncio.get_event_loop().time()
        if now >= deadline:
            logger.info(
                "guardrails.checkpoint_and_rollback: window closed without verdict for %s "
                "(attempts=%d); treating as verified",
                action.label or action.repo,
                attempts,
            )
            return CheckpointResult(
                outcome=RollbackOutcome.VERIFIED,
                attempts=attempts,
                reason="window_closed_pending",
            )

        # Sleep the smaller of poll_interval and remaining window.
        sleep_for = min(poll_interval_seconds, max(0.0, deadline - now))
        await asyncio.sleep(sleep_for)


__all__ = [
    "CheckpointResult",
    "CheckpointedAction",
    "RollbackFn",
    "RollbackOutcome",
    "VerifyFn",
    "VerifyResult",
    "checkpoint_and_rollback",
]
