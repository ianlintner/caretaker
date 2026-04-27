"""Tests for the streaming shipper's config-from-env helper."""

from __future__ import annotations

import pytest

from caretaker.runs.shipper import _DEFAULT_AUDIENCE, _config_from_env


@pytest.fixture()
def _backend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARETAKER_BACKEND_URL", "https://caretaker.example.com")


@pytest.mark.usefixtures("_backend_url")
def test_audience_defaults_when_var_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARETAKER_OIDC_AUDIENCE", raising=False)
    cfg = _config_from_env(mode="full", tail=True, event_type=None, event_payload=None)
    assert cfg.audience == _DEFAULT_AUDIENCE


@pytest.mark.usefixtures("_backend_url")
def test_audience_defaults_when_var_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # GitHub Actions sets the env var to "" when the repo variable is unset.
    # os.environ.get(KEY, default) would return "" here — the `or` fix prevents that.
    monkeypatch.setenv("CARETAKER_OIDC_AUDIENCE", "")
    cfg = _config_from_env(mode="full", tail=True, event_type=None, event_payload=None)
    assert cfg.audience == _DEFAULT_AUDIENCE


@pytest.mark.usefixtures("_backend_url")
def test_audience_respects_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARETAKER_OIDC_AUDIENCE", "my-custom-audience")
    cfg = _config_from_env(mode="full", tail=True, event_type=None, event_payload=None)
    assert cfg.audience == "my-custom-audience"


@pytest.mark.usefixtures("_backend_url")
def test_missing_backend_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARETAKER_BACKEND_URL", raising=False)
    monkeypatch.setenv("CARETAKER_BACKEND_URL", "")
    with pytest.raises(RuntimeError, match="CARETAKER_BACKEND_URL is required"):
        _config_from_env(mode="full", tail=True, event_type=None, event_payload=None)
