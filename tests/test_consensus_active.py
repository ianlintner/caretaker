"""Tests for the process-wide ConsensusEngine holder."""

from __future__ import annotations

from caretaker.consensus import active
from caretaker.consensus.engine import ConsensusEngine, EngineConfig
from caretaker.consensus.provider_pool import ProviderPool


def _engine() -> ConsensusEngine:
    pool = ProviderPool({"fast": "fake-fast"})
    return ConsensusEngine(
        config=EngineConfig(pool=pool, sites={}),
        claude=object(),  # type: ignore[arg-type]
    )


def test_get_returns_none_when_unconfigured() -> None:
    active.reset_for_tests()
    assert active.get_active_engine() is None


def test_configure_installs_engine() -> None:
    active.reset_for_tests()
    engine = _engine()
    active.configure(engine)
    assert active.get_active_engine() is engine


def test_reset_clears_engine() -> None:
    active.configure(_engine())
    active.reset_for_tests()
    assert active.get_active_engine() is None
