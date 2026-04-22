"""Tests for caretaker.guardrails.rollback."""

from __future__ import annotations

import asyncio

import pytest

from caretaker.guardrails.rollback import (
    CheckpointedAction,
    RollbackOutcome,
    checkpoint_and_rollback,
)


@pytest.mark.asyncio
async def test_disabled_returns_skipped_without_calling_verify() -> None:
    verify_called = False
    rollback_called = False

    async def verify() -> bool | None:
        nonlocal verify_called
        verify_called = True
        return False

    async def rollback() -> None:
        nonlocal rollback_called
        rollback_called = True

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="disabled"),
        verify=verify,
        rollback=rollback,
        enabled=False,
    )
    assert result.outcome is RollbackOutcome.SKIPPED
    assert not verify_called
    assert not rollback_called


@pytest.mark.asyncio
async def test_verify_returns_true_exits_clean() -> None:
    rollback_called = False

    async def verify() -> bool | None:
        return True

    async def rollback() -> None:
        nonlocal rollback_called
        rollback_called = True

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="green"),
        verify=verify,
        rollback=rollback,
        window_seconds=1,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.VERIFIED
    assert result.attempts == 1
    assert not rollback_called


@pytest.mark.asyncio
async def test_verify_returns_false_fires_rollback() -> None:
    rollback_invocations: list[int] = []

    async def verify() -> bool | None:
        return False

    async def rollback() -> None:
        rollback_invocations.append(1)

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="red"),
        verify=verify,
        rollback=rollback,
        window_seconds=1,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.ROLLED_BACK
    assert len(rollback_invocations) == 1


@pytest.mark.asyncio
async def test_rollback_failure_surfaces() -> None:
    async def verify() -> bool | None:
        return False

    async def rollback() -> None:
        raise RuntimeError("revert PR creation failed")

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="rollback-bad"),
        verify=verify,
        rollback=rollback,
        window_seconds=1,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.ROLLBACK_FAILED
    assert "revert PR creation failed" in result.reason


@pytest.mark.asyncio
async def test_pending_then_green_eventually_verifies() -> None:
    counter = {"n": 0}

    async def verify() -> bool | None:
        counter["n"] += 1
        if counter["n"] < 3:
            return None
        return True

    async def rollback() -> None:
        raise AssertionError("rollback must not fire on a verified window")

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="pending-then-green"),
        verify=verify,
        rollback=rollback,
        window_seconds=5,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.VERIFIED
    assert result.attempts >= 3


@pytest.mark.asyncio
async def test_pending_exhausts_window_treats_as_verified() -> None:
    # When verify never returns a verdict we close the window treating the
    # merge as clean — the backstop is the nightly reconciliation pass.
    async def verify() -> bool | None:
        return None

    async def rollback() -> None:
        raise AssertionError("rollback must not fire on window-closed-pending")

    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="pending-forever"),
        verify=verify,
        rollback=rollback,
        window_seconds=0,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.VERIFIED
    assert result.reason == "window_closed_pending"


@pytest.mark.asyncio
async def test_verify_exception_is_swallowed_and_retried() -> None:
    attempts = {"n": 0}

    async def verify() -> bool | None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient GitHub 500")
        return True

    async def rollback() -> None:
        raise AssertionError("rollback must not fire on transient verify error")

    # window >0 so the wrapper gets a second attempt after the first raise.
    result = await checkpoint_and_rollback(
        action=CheckpointedAction(repo="o/r", label="flaky-verify"),
        verify=verify,
        rollback=rollback,
        window_seconds=5,
        poll_interval_seconds=0,
    )
    assert result.outcome is RollbackOutcome.VERIFIED
    assert attempts["n"] >= 2


@pytest.mark.asyncio
async def test_negative_window_rejects_immediately_on_pending() -> None:
    # Sanity: clamping window to 0 means pending verdicts short-circuit.
    async def verify() -> bool | None:
        return None

    async def rollback() -> None:  # pragma: no cover
        raise AssertionError

    result = await asyncio.wait_for(
        checkpoint_and_rollback(
            action=CheckpointedAction(repo="o/r", label="neg-window"),
            verify=verify,
            rollback=rollback,
            window_seconds=-5,
            poll_interval_seconds=0,
        ),
        timeout=2.0,
    )
    assert result.outcome is RollbackOutcome.VERIFIED
