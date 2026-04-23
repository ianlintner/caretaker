"""Tests for the ``caretaker doctor --llm-probe`` preflight subcommand.

The LLM-probe is the online counterpart to ``--bootstrap-check``: it
resolves every distinct model string in ``default_model /
feature_models[*].model / fallback_models`` and pings each endpoint
with a 1-token ``litellm.acompletion`` call. Its job is to catch
misconfigurations that today only surface at the first feature-fire
(typos in model strings, rotated-but-not-updated keys, wrong Azure
deployment ids).

These tests pin down the severity contract so the CI exit code stays
predictable:

* missing env → FAIL on primary, WARN on fallback-only, and CRUCIALLY
  ``acompletion`` is NOT called so no paid token is wasted on an
  obviously-misconfigured model.
* 401/403/404 on a primary model → FAIL.
* timeout → WARN (transient).
* any fallback-only model failure → WARN, never FAIL.
* ``litellm`` import missing → single FAIL row, exit 1 (the operator's
  remediation is ``pip install litellm``; we surface that as a row,
  not a traceback).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from caretaker.cli import main as cli_main
from caretaker.config import MaintainerConfig
from caretaker.doctor import (
    Severity,
    run_llm_probe,
    run_llm_probe_sync,
)

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator


# ── Fixtures ──────────────────────────────────────────────────────────


def _load_config(overrides: dict[str, Any] | None = None) -> MaintainerConfig:
    data: dict[str, Any] = {"version": "v1"}
    if overrides:
        data.update(overrides)
    return MaintainerConfig.model_validate(data)


def _write_config(path: pathlib.Path, overrides: dict[str, Any] | None = None) -> pathlib.Path:
    import yaml

    data: dict[str, Any] = {"version": "v1"}
    if overrides:
        data.update(overrides)
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(autouse=True)
def _no_process_env_leak(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scrub LLM-related env vars so probe tests start hermetic."""
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_AI_API_KEY",
        "AZURE_AI_API_BASE",
        "AZURE_API_KEY",
        "AZURE_API_BASE",
        "VERTEX_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OLLAMA_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


class _StubResponse:
    """Minimal shape LiteLLM responses expose; enough for the probe."""

    def __init__(self, text: str = "2") -> None:
        self.choices = [type("_Choice", (), {"message": type("_Msg", (), {"content": text})()})()]


def _stub_success(calls: list[dict[str, Any]]) -> Any:
    """Return an ``acompletion`` stub that always succeeds."""

    async def _acompletion(**kwargs: Any) -> _StubResponse:
        calls.append(kwargs)
        return _StubResponse()

    return _acompletion


def _stub_raises(exc: BaseException, calls: list[dict[str, Any]]) -> Any:
    """Return an ``acompletion`` stub that always raises ``exc``."""

    async def _acompletion(**kwargs: Any) -> _StubResponse:
        calls.append(kwargs)
        raise exc

    return _acompletion


# ── litellm-not-installed contract ────────────────────────────────────


def test_litellm_not_installed_returns_single_fail_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``import litellm`` raises, emit one FAIL row + exit 1 — not a traceback.

    The operator's only remediation is ``pip install litellm``; making
    that the detail of a CheckResult row keeps the JSON envelope stable
    and the CI failure actionable.
    """
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
            }
        }
    )

    def _boom(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "litellm":
            raise ImportError("No module named 'litellm' (test stub)")
        return _real_import_module(name, *args, **kwargs)

    import importlib

    _real_import_module = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", _boom)

    report = run_llm_probe_sync(config)
    assert [r.name for r in report.results] == ["litellm"]
    assert report.results[0].severity is Severity.FAIL
    assert "pip install litellm" in report.results[0].detail
    assert report.has_failures is True


# ── Happy path ────────────────────────────────────────────────────────


def test_all_models_have_env_and_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every configured model reachable → all OK rows, no FAILs, exit 0."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
                "fallback_models": ["openai/gpt-4o"],
            }
        }
    )
    # Both providers need their keys present so the probe actually fires.
    monkeypatch.setenv("AZURE_AI_API_KEY", "x")
    monkeypatch.setenv("AZURE_AI_API_BASE", "https://example.azure.com")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    calls: list[dict[str, Any]] = []
    report = asyncio.run(run_llm_probe(config, acompletion=_stub_success(calls)))

    # Both models were probed and both got OK rows.
    names = [r.name for r in report.results]
    assert "azure_ai/gpt-4.1-mini" in names
    assert "openai/gpt-4o" in names
    assert all(r.severity is Severity.OK for r in report.results)
    # Each call used max_tokens=1 (the cheap-ping contract).
    assert all(kw.get("max_tokens") == 1 for kw in calls)
    # Exactly one call per distinct model.
    probed_models = sorted(kw["model"] for kw in calls)
    assert probed_models == ["azure_ai/gpt-4.1-mini", "openai/gpt-4o"]


# ── Missing env short-circuits before the network ────────────────────


def test_missing_env_emits_fail_before_probe() -> None:
    """Missing AZURE_AI_API_KEY → FAIL row, and NO acompletion call.

    The whole point of the env-var pre-check is to avoid paying for a
    token when we already know the call will 401; assert the stub was
    never invoked.
    """
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "azure_ai/gpt-4.1-mini",
            }
        }
    )

    calls: list[dict[str, Any]] = []
    report = asyncio.run(run_llm_probe(config, env={}, acompletion=_stub_success(calls)))

    assert calls == [], "acompletion must not be called when env vars are missing"
    row = next(r for r in report.results if r.name == "azure_ai/gpt-4.1-mini")
    assert row.severity is Severity.FAIL
    assert "AZURE_AI_API_KEY" in row.detail


# ── 401 on primary model is FAIL (not WARN) ──────────────────────────


def test_probe_401_emits_fail_not_warn() -> None:
    """Authentication failures on primary models are terminal, not transient."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "openai/gpt-4o",
            }
        }
    )

    class _FakeAuthError(Exception):
        def __init__(self) -> None:
            super().__init__("AuthenticationError: Error code: 401 Unauthorized")
            self.status_code = 401

    calls: list[dict[str, Any]] = []
    report = asyncio.run(
        run_llm_probe(
            config,
            env={"OPENAI_API_KEY": "sk-wrong"},
            acompletion=_stub_raises(_FakeAuthError(), calls),
        )
    )

    row = next(r for r in report.results if r.name == "openai/gpt-4o")
    assert row.severity is Severity.FAIL
    assert "401" in row.detail
    # The probe *was* called — env was present, so we exercised auth.
    assert len(calls) == 1


# ── Timeout is transient → WARN ──────────────────────────────────────


def test_probe_timeout_emits_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout does NOT flip CI red — endpoints have bad moments."""
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "openai/gpt-4o",
                "timeout_seconds": 0.01,  # tight to keep the test fast
            }
        }
    )

    async def _hangs(**_kwargs: Any) -> Any:
        await asyncio.sleep(10)  # would be killed by wait_for
        return _StubResponse()

    report = asyncio.run(
        run_llm_probe(
            config,
            env={"OPENAI_API_KEY": "sk-x"},
            acompletion=_hangs,
        )
    )

    row = next(r for r in report.results if r.name == "openai/gpt-4o")
    assert row.severity is Severity.WARN
    assert "timed out" in row.detail.lower()
    assert report.has_failures is False


# ── JSON output envelope ─────────────────────────────────────────────


def test_json_output_shape_stable(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The --json payload carries ``checks`` + counts; no new top-level keys.

    We follow the existing ``DoctorReport.to_dict`` envelope
    (``status`` / ``counts`` / ``checks``) — the contract is "no new
    top-level keys when --llm-probe is used" so CI consumers of the
    existing shape keep working.
    """
    cfg = _write_config(
        tmp_path / "config.yml",
        {
            "llm": {
                "provider": "litellm",
                "default_model": "openai/gpt-4o",
            }
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")

    # Stub litellm.acompletion at the module level so the CLI path
    # (which calls importlib.import_module) picks it up.
    class _FakeLiteLLM:
        @staticmethod
        async def acompletion(**_kwargs: Any) -> _StubResponse:
            return _StubResponse()

    monkeypatch.setitem(__import__("sys").modules, "litellm", _FakeLiteLLM)

    try:
        runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["doctor", "--config", str(cfg), "--llm-probe", "--json"],
    )
    assert result.exit_code == 0, result.output
    combined = result.output
    first_brace = combined.find("\n{")
    if first_brace == -1 and combined.startswith("{"):
        first_brace = 0
    else:
        first_brace += 1
    data = json.loads(combined[first_brace:])
    # Envelope assertions — exactly the existing top-level keys.
    assert set(data.keys()) == {"status", "counts", "checks"}
    assert isinstance(data["checks"], list)
    assert set(data["counts"].keys()) == {"OK", "WARN", "FAIL"}
    assert data["status"] in {"ok", "warn", "fail"}


# ── Fallback-chain failures never escalate to FAIL ───────────────────


def test_fallback_model_probe_is_warn_on_fail_not_error() -> None:
    """A 404 on a fallback-only model is WARN, NOT FAIL.

    The fallback chain is best-effort; a broken link doesn't block the
    primary request path, so it mustn't flip the CI gate. The detail
    text must explain the reasoning so operators don't mistake it for
    a bug.
    """
    config = _load_config(
        {
            "llm": {
                "provider": "litellm",
                "default_model": "openai/gpt-4o",
                "fallback_models": ["azure_ai/broken-deployment"],
            }
        }
    )

    class _FakeNotFoundError(Exception):
        def __init__(self) -> None:
            super().__init__("NotFoundError: 404 Deployment not found")
            self.status_code = 404

    async def _selective(**kwargs: Any) -> _StubResponse:
        if kwargs["model"] == "azure_ai/broken-deployment":
            raise _FakeNotFoundError()
        return _StubResponse()

    report = asyncio.run(
        run_llm_probe(
            config,
            env={
                "OPENAI_API_KEY": "sk-x",
                "AZURE_AI_API_KEY": "x",
                "AZURE_AI_API_BASE": "https://example.azure.com",
            },
            acompletion=_selective,
        )
    )

    primary = next(r for r in report.results if r.name == "openai/gpt-4o")
    fallback = next(r for r in report.results if r.name == "azure_ai/broken-deployment")
    assert primary.severity is Severity.OK
    # KEY contract: a broken fallback is WARN, NOT FAIL.
    assert fallback.severity is Severity.WARN
    # The detail must explain why so operators don't file a "doctor bug".
    assert "fallback" in fallback.detail.lower()
    # And the overall report must not flip CI red.
    assert report.has_failures is False
