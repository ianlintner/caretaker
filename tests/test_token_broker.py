"""Tests for the Redis-backed installation token broker."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from caretaker.state.token_broker import build_token_broker


class TestBuildTokenBroker:
    def test_returns_none_when_app_id_not_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CARETAKER_GITHUB_APP_ID", raising=False)
        broker = build_token_broker()
        assert broker is None

    def test_returns_none_when_app_id_not_integer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "not-an-int")
        broker = build_token_broker()
        assert broker is None

    def test_returns_none_when_no_private_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "12345")
        monkeypatch.delenv("CARETAKER_GITHUB_APP_PRIVATE_KEY", raising=False)
        monkeypatch.delenv("CARETAKER_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
        broker = build_token_broker()
        assert broker is None

    def test_returns_minter_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Minimal test — just verifies build_token_broker returns an object."""
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "42")
        monkeypatch.setenv("CARETAKER_GITHUB_APP_PRIVATE_KEY", "fake-pem-content")
        monkeypatch.delenv("REDIS_URL", raising=False)

        with patch("caretaker.state.token_broker.AppJWTSigner") as mock_signer:
            mock_signer.return_value = object()
            broker = build_token_broker()
            assert broker is not None
            mock_signer.assert_called_once_with(app_id=42, private_key_pem="fake-pem-content")

    def test_uses_in_process_cache_without_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "42")
        monkeypatch.setenv("CARETAKER_GITHUB_APP_PRIVATE_KEY", "fake-pem")
        monkeypatch.delenv("REDIS_URL", raising=False)

        from caretaker.github_app.installation_tokens import InstallationTokenCache
        from caretaker.state.token_broker import RedisTokenCache

        with patch("caretaker.state.token_broker.AppJWTSigner"):
            broker = build_token_broker()
            assert broker is not None
            assert not isinstance(broker._cache, RedisTokenCache)
            assert isinstance(broker._cache, InstallationTokenCache)

    def test_uses_redis_cache_when_redis_url_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "42")
        monkeypatch.setenv("CARETAKER_GITHUB_APP_PRIVATE_KEY", "fake-pem")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")

        from caretaker.state.token_broker import RedisTokenCache

        with patch("caretaker.state.token_broker.AppJWTSigner"):
            broker = build_token_broker()
            assert broker is not None
            assert isinstance(broker._cache, RedisTokenCache)

    def test_reads_private_key_from_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "key.pem"
        key_file.write_text("file-pem-content")
        monkeypatch.setenv("CARETAKER_GITHUB_APP_ID", "99")
        monkeypatch.delenv("CARETAKER_GITHUB_APP_PRIVATE_KEY", raising=False)
        monkeypatch.setenv("CARETAKER_GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
        monkeypatch.delenv("REDIS_URL", raising=False)

        with patch("caretaker.state.token_broker.AppJWTSigner") as mock_signer:
            mock_signer.return_value = object()
            broker = build_token_broker()
            assert broker is not None
            mock_signer.assert_called_once_with(app_id=99, private_key_pem="file-pem-content")
