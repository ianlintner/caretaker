"""Tests for the memory backend factory."""

from __future__ import annotations

import pytest

from caretaker.config import MaintainerConfig
from caretaker.state.backends.factory import build_memory_backend
from caretaker.state.backends.sqlite_backend import SQLiteMemoryBackend


@pytest.fixture
def base_config() -> MaintainerConfig:
    return MaintainerConfig()


class TestBuildMemoryBackend:
    def test_returns_none_when_disabled(self, base_config: MaintainerConfig) -> None:
        base_config.memory_store.enabled = False
        assert build_memory_backend(base_config) is None

    def test_returns_sqlite_by_default(self, base_config: MaintainerConfig) -> None:
        base_config.memory_store.enabled = True
        base_config.memory_store.db_path = ":memory:"
        backend = build_memory_backend(base_config)
        assert isinstance(backend, SQLiteMemoryBackend)

    def test_falls_back_to_sqlite_when_postgres_not_enabled(
        self, base_config: MaintainerConfig
    ) -> None:
        base_config.memory_store.enabled = True
        base_config.memory_store.backend = "postgres"  # type: ignore[assignment]
        base_config.postgres.enabled = False
        base_config.memory_store.db_path = ":memory:"
        backend = build_memory_backend(base_config)
        assert isinstance(backend, SQLiteMemoryBackend)

    def test_returns_postgres_backend_when_configured(
        self, base_config: MaintainerConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
        base_config.memory_store.enabled = True
        base_config.memory_store.backend = "postgres"  # type: ignore[assignment]
        base_config.postgres.enabled = True

        from caretaker.state.backends.postgres_backend import PostgresMemoryBackend

        backend = build_memory_backend(base_config)
        assert isinstance(backend, PostgresMemoryBackend)

    def test_sqlite_backend_is_functional(self, base_config: MaintainerConfig) -> None:
        base_config.memory_store.enabled = True
        base_config.memory_store.db_path = ":memory:"
        backend = build_memory_backend(base_config)
        assert backend is not None
        backend.set("ns", "key", "val")
        assert backend.get("ns", "key") == "val"
