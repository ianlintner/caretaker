"""Tests for the AuditLogWriter."""

from __future__ import annotations

import pytest

from caretaker.state.audit_log import AuditLogWriter


class TestAuditLogWriterDisabled:
    """When disabled, no MongoDB connection is attempted."""

    @pytest.mark.asyncio
    async def test_record_does_not_raise_when_disabled(self) -> None:
        writer = AuditLogWriter(enabled=False)
        # Should not raise even without a DB
        await writer.record(run_id="r1", agent_id="test", outcome="success")

    @pytest.mark.asyncio
    async def test_close_does_not_raise_when_disabled(self) -> None:
        writer = AuditLogWriter(enabled=False)
        await writer.close()

    @pytest.mark.asyncio
    async def test_record_with_all_fields(self) -> None:
        writer = AuditLogWriter(enabled=False)
        await writer.record(
            run_id="r1",
            agent_id="security",
            outcome="success",
            tool="GitHub.list_issues",
            llm_model="gpt-4o",
            latency_ms=150,
            cost_usd=0.001,
            prompt_id="p1",
            response_id="resp-1",
            extra={"repo": "owner/repo"},
        )


class TestAuditLogWriterFromConfig:
    def test_disabled_when_mongo_not_enabled(self) -> None:
        from caretaker.config import MaintainerConfig

        config = MaintainerConfig()
        config.mongo.enabled = False
        config.audit_log.enabled = True
        writer = AuditLogWriter.from_config(config)
        assert writer._enabled is False

    def test_disabled_when_audit_log_not_enabled(self) -> None:
        from caretaker.config import MaintainerConfig

        config = MaintainerConfig()
        config.mongo.enabled = True
        config.audit_log.enabled = False
        writer = AuditLogWriter.from_config(config)
        assert writer._enabled is False

    def test_enabled_when_both_configured(self) -> None:
        from caretaker.config import MaintainerConfig

        config = MaintainerConfig()
        config.mongo.enabled = True
        config.audit_log.enabled = True
        writer = AuditLogWriter.from_config(config)
        assert writer._enabled is True

    def test_disabled_for_non_config_input(self) -> None:
        writer = AuditLogWriter.from_config(object())
        assert writer._enabled is False

    @pytest.mark.asyncio
    async def test_no_db_connection_without_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AuditLogWriter should not crash when MONGODB_URL is absent."""
        monkeypatch.delenv("MONGODB_URL", raising=False)
        writer = AuditLogWriter(enabled=True, mongodb_url_env="MONGODB_URL")
        # _ensure_collection returns None when env var not set
        conn = await writer._ensure_collection()
        assert conn is None
        await writer.close()
