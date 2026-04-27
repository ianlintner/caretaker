"""Tests for the ``caretaker doctor`` preflight subcommand."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from caretaker.cli import main as cli_main
from caretaker.config import MaintainerConfig
from caretaker.doctor import (
    CheckResult,
    DoctorReport,
    Severity,
    check_bootstrap_env_secrets,
    check_config_parse,
    check_env_secrets,
    check_external_services,
    check_github_scopes,
    check_import_ok,
    check_version_pin,
    collect_env_references,
    render_table,
    run_bootstrap_check,
    run_doctor,
)

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator


# ── Fixtures ──────────────────────────────────────────────────────────


def _write_config(path: pathlib.Path, overrides: dict[str, Any] | None = None) -> pathlib.Path:
    """Write a minimal valid YAML config with optional block overrides."""
    import yaml

    data: dict[str, Any] = {"version": "v1"}
    if overrides:
        data.update(overrides)
    path.write_text(yaml.safe_dump(data))
    return path


def _load_config(overrides: dict[str, Any] | None = None) -> MaintainerConfig:
    data: dict[str, Any] = {"version": "v1"}
    if overrides:
        data.update(overrides)
    return MaintainerConfig.model_validate(data)


@pytest.fixture(autouse=True)
def _no_process_env_leak(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Scrub env vars the doctor inspects so tests are hermetic."""
    for name in (
        "GITHUB_TOKEN",
        "COPILOT_PAT",
        "GITHUB_REPOSITORY",
        "ANTHROPIC_API_KEY",
        "MONGODB_URL",
        "REDIS_URL",
        "NEO4J_URL",
        "NEO4J_AUTH",
        "CARETAKER_FLEET_SECRET",
        "CARETAKER_ADMIN_OIDC_CLIENT_ID",
        "CARETAKER_ADMIN_OIDC_CLIENT_SECRET",
        "CARETAKER_ADMIN_SESSION_SECRET",
        "CARETAKER_GITHUB_APP_PRIVATE_KEY",
        "CARETAKER_GITHUB_APP_WEBHOOK_SECRET",
        "CARETAKER_GITHUB_APP_CLIENT_ID",
        "CARETAKER_GITHUB_APP_CLIENT_SECRET",
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "OAUTH2_CLIENT_ID",
        "OAUTH2_CLIENT_SECRET",
        "OAUTH2_TOKEN_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# ── collect_env_references ────────────────────────────────────────────


class TestCollectEnvReferences:
    def test_includes_all_known_env_names_by_default(self) -> None:
        config = _load_config()
        refs = collect_env_references(config)
        names = {r.env_name for r in refs}
        assert "MONGODB_URL" in names
        assert "REDIS_URL" in names
        assert "CARETAKER_GITHUB_APP_PRIVATE_KEY" in names
        assert "CARETAKER_ADMIN_OIDC_CLIENT_ID" in names
        assert "NEO4J_URL" in names
        assert "NEO4J_AUTH" in names
        # CARETAKER_FLEET_SECRET (legacy HMAC) was removed in v0.20.1;
        # OAuth2 is the only supported heartbeat auth mode.
        assert "CARETAKER_FLEET_SECRET" not in names

    def test_fleet_registry_oauth2_envs_referenced_when_enabled(self) -> None:
        config = _load_config({"fleet_registry": {"enabled": True, "oauth2": {"enabled": True}}})
        refs = collect_env_references(config)
        names = {r.env_name for r in refs if r.owner_enabled}
        assert {"OAUTH2_CLIENT_ID", "OAUTH2_CLIENT_SECRET", "OAUTH2_TOKEN_URL"} <= names

    def test_owner_enabled_tracks_block_state(self) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        refs = collect_env_references(config)
        mongo = next(r for r in refs if r.env_name == "MONGODB_URL")
        assert mongo.owner_enabled is True

        config2 = _load_config({"mongo": {"enabled": False}})
        refs2 = collect_env_references(config2)
        mongo2 = next(r for r in refs2 if r.env_name == "MONGODB_URL")
        assert mongo2.owner_enabled is False


# ── check_env_secrets ─────────────────────────────────────────────────


class TestCheckEnvSecrets:
    def test_missing_on_enabled_block_is_fail(self) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        env = {"GITHUB_TOKEN": "ghs_fake"}
        rows = check_env_secrets(config, env)
        mongo_row = next(r for r in rows if r.name == "MONGODB_URL")
        assert mongo_row.severity is Severity.FAIL

    def test_missing_on_disabled_block_is_warn(self) -> None:
        config = _load_config()  # mongo defaults to enabled=False
        env = {"GITHUB_TOKEN": "ghs_fake"}
        rows = check_env_secrets(config, env)
        mongo_row = next(r for r in rows if r.name == "MONGODB_URL")
        assert mongo_row.severity is Severity.WARN

    def test_present_env_is_ok(self) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        env = {"GITHUB_TOKEN": "ghs_fake", "MONGODB_URL": "mongodb://localhost:27017"}
        rows = check_env_secrets(config, env)
        mongo_row = next(r for r in rows if r.name == "MONGODB_URL")
        assert mongo_row.severity is Severity.OK

    def test_missing_github_token_fails(self) -> None:
        config = _load_config()
        rows = check_env_secrets(config, {})
        token_row = next(r for r in rows if r.name == "GITHUB_TOKEN")
        assert token_row.severity is Severity.FAIL

    def test_copilot_pat_alone_satisfies_github_token(self) -> None:
        config = _load_config()
        rows = check_env_secrets(config, {"COPILOT_PAT": "ghp_fake"})
        token_row = next(r for r in rows if r.name == "GITHUB_TOKEN")
        assert token_row.severity is Severity.OK

    def test_anthropic_key_expected_when_provider_is_anthropic(self) -> None:
        config = _load_config()  # provider default = anthropic
        rows = check_env_secrets(config, {"GITHUB_TOKEN": "ghs_fake"})
        ant_row = next(r for r in rows if r.name == "ANTHROPIC_API_KEY")
        assert ant_row.severity is Severity.FAIL


# ── check_github_scopes ───────────────────────────────────────────────


def _make_mock_github() -> tuple[Any, list[tuple[str, str]]]:
    """Return a fake GitHubClient + the list of recorded requests."""
    calls: list[tuple[str, str]] = []

    class _FakeResponse:
        def __init__(
            self,
            status_code: int,
            body: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.status_code = status_code
            self._body = body or {}
            self.headers = headers or {}
            self.text = json.dumps(self._body)

        def json(self) -> dict[str, Any]:
            return self._body

    class _FakeClient:
        async def request(self, method: str, path: str, **_kw: Any) -> _FakeResponse:
            calls.append((method, path))
            if path == "/user":
                return _FakeResponse(
                    200, {"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"}
                )
            if path.endswith("/dependabot/alerts"):
                return _FakeResponse(403, {"message": "Resource not accessible by integration"})
            if path.endswith("/code-scanning/alerts"):
                return _FakeResponse(200, [])
            if "/issues" in path:
                return _FakeResponse(200, [])
            if "/pulls" in path:
                return _FakeResponse(200, [])
            if "/actions/runs" in path:
                return _FakeResponse(200, [])
            return _FakeResponse(200, {"owner": {"login": "o"}, "name": "r"})

    class _FakeCreds:
        async def default_token(self, *, installation_id: int | None = None) -> str:  # noqa: ARG002
            return "tok"

        async def copilot_token(self, *, installation_id: int | None = None) -> str:  # noqa: ARG002
            return "tok"

    class _FakeGithub:
        def __init__(self) -> None:
            self._creds = _FakeCreds()
            self._client = _FakeClient()

        async def close(self) -> None:
            return None

    return _FakeGithub(), calls


class TestCheckGithubScopes:
    @pytest.mark.asyncio
    async def test_requires_github_repository_env(self) -> None:
        config = _load_config()
        github, _calls = _make_mock_github()
        rows = await check_github_scopes(config, github, env={})
        assert any(r.name == "GITHUB_REPOSITORY" and r.severity is Severity.WARN for r in rows)

    @pytest.mark.asyncio
    async def test_declared_scopes_reported(self) -> None:
        config = _load_config()
        github, _calls = _make_mock_github()
        rows = await check_github_scopes(config, github, env={"GITHUB_REPOSITORY": "o/r"})
        declared = next(r for r in rows if r.name == "declared scopes")
        assert "repo" in (declared.detail or "")

    @pytest.mark.asyncio
    async def test_403_scope_gap_is_fail(self) -> None:
        config = _load_config(
            {
                "security_agent": {
                    "enabled": True,
                    "include_dependabot": True,
                    "include_code_scanning": False,
                    "include_secret_scanning": False,
                }
            }
        )
        github, calls = _make_mock_github()
        rows = await check_github_scopes(config, github, env={"GITHUB_REPOSITORY": "o/r"})
        depa = next(r for r in rows if "dependabot/alerts" in r.name)
        assert depa.severity is Severity.FAIL
        assert any(path.endswith("/dependabot/alerts") for _m, path in calls)

    @pytest.mark.asyncio
    async def test_403_rate_limit_is_warn(self) -> None:
        """A 403 caused by API rate limiting is transient and must not block the run."""
        calls: list[tuple[str, str]] = []
        _rate_limit_message = (
            "API rate limit exceeded for installation ID 124850811. "
            "If you reach out to GitHub Support for help, please include "
            "the request ID and timestamp."
        )

        class _RateLimitClient:
            async def request(self, method: str, path: str, **_kw: Any) -> Any:
                calls.append((method, path))

                class _Resp:
                    status_code = 403
                    text = json.dumps({"message": _rate_limit_message})

                    def json(self) -> dict[str, Any]:
                        return {"message": _rate_limit_message}

                return _Resp()

        class _FakeCreds:
            async def default_token(self, *, installation_id: int | None = None) -> str:  # noqa: ARG002
                return "tok"

        class _FakeGithub:
            def __init__(self) -> None:
                self._creds = _FakeCreds()
                self._client = _RateLimitClient()

            async def close(self) -> None:
                return None

        config = _load_config()
        github = _FakeGithub()
        rows = await check_github_scopes(config, github, env={"GITHUB_REPOSITORY": "o/r"})
        # All repo-scoped probes return 403 rate-limit — none should be FAIL.
        probe_rows = [r for r in rows if r.category == "github" and r.name != "GET /user"]
        assert all(r.severity is Severity.WARN for r in probe_rows), (
            f"Expected all probe rows to be WARN, got: {probe_rows}"
        )
        assert any("rate-limit" in (r.detail or "") for r in probe_rows)

    @pytest.mark.asyncio
    async def test_disabled_security_agent_skips_probes(self) -> None:
        config = _load_config({"security_agent": {"enabled": False}})
        github, calls = _make_mock_github()
        await check_github_scopes(config, github, env={"GITHUB_REPOSITORY": "o/r"})
        probed = {path for _m, path in calls}
        assert not any("/dependabot/alerts" in p for p in probed)


# ── check_external_services ───────────────────────────────────────────


class TestCheckExternalServices:
    def test_skips_disabled_blocks(self) -> None:
        config = _load_config()
        rows = check_external_services(config, env={}, strict=False)
        assert rows == []

    def test_strict_upgrades_warn_to_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        env = {"MONGODB_URL": "mongodb://unreachable.invalid:27017"}

        def _boom(_host: str, _port: int, *, timeout: float = 2.0) -> tuple[bool, str]:
            return False, "unreachable: test"

        monkeypatch.setattr("caretaker.doctor._check_tcp_reachable", _boom)

        warn_rows = check_external_services(config, env=env, strict=False)
        assert any(r.severity is Severity.WARN for r in warn_rows)
        fail_rows = check_external_services(config, env=env, strict=True)
        assert any(r.severity is Severity.FAIL for r in fail_rows)

    def test_reachable_service_reports_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        env = {"MONGODB_URL": "mongodb://localhost:27017"}

        def _ok(_host: str, _port: int, *, timeout: float = 2.0) -> tuple[bool, str]:
            return True, "localhost:27017 reachable"

        monkeypatch.setattr("caretaker.doctor._check_tcp_reachable", _ok)
        rows = check_external_services(config, env=env, strict=False)
        assert rows and rows[0].severity is Severity.OK


# ── Report + rendering ────────────────────────────────────────────────


class TestDoctorReport:
    def test_has_failures_and_counts(self) -> None:
        report = DoctorReport(
            results=[
                CheckResult("secrets", "A", Severity.OK, "ok"),
                CheckResult("secrets", "B", Severity.WARN, "meh"),
                CheckResult("secrets", "C", Severity.FAIL, "nope"),
            ]
        )
        assert report.has_failures is True
        assert report.has_warnings is True
        assert report.summary_counts() == {"OK": 1, "WARN": 1, "FAIL": 1}

    def test_to_dict_shape(self) -> None:
        report = DoctorReport(results=[CheckResult("s", "x", Severity.OK, "ok")])
        data = report.to_dict()
        assert data["status"] == "ok"
        assert isinstance(data["checks"], list)
        assert data["counts"] == {"OK": 1, "WARN": 0, "FAIL": 0}

    def test_render_table_contains_header(self) -> None:
        report = DoctorReport(results=[CheckResult("s", "x", Severity.OK, "ok")])
        table = render_table(report)
        assert "CATEGORY" in table
        assert "OK=1" in table


# ── run_doctor orchestration ──────────────────────────────────────────


class TestRunDoctor:
    @pytest.mark.asyncio
    async def test_skip_github_skips_scope_checks(self) -> None:
        config = _load_config()
        report = await run_doctor(
            config,
            env={"GITHUB_TOKEN": "ghs_fake"},
            skip_github=True,
        )
        assert not any(r.category == "github" for r in report.results)

    @pytest.mark.asyncio
    async def test_github_probes_invoked(self) -> None:
        config = _load_config()
        github, calls = _make_mock_github()
        env = {"GITHUB_TOKEN": "ghs_fake", "GITHUB_REPOSITORY": "o/r"}
        report = await run_doctor(config, env=env, github=github, skip_github=False)
        assert any(r.category == "github" for r in report.results)
        assert any(path == "/user" for _m, path in calls)


# ── CLI integration ───────────────────────────────────────────────────


class TestDoctorCLI:
    def test_missing_config_exits_2(self, tmp_path: pathlib.Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["doctor", "--config", str(tmp_path / "nonexistent.yml"), "--skip-github"],
        )
        assert result.exit_code == 2

    def test_ok_run_exits_0(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["doctor", "--config", str(cfg), "--skip-github"],
        )
        assert result.exit_code == 0

    def test_fail_on_missing_secret_exits_1(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_config(tmp_path / "config.yml", {"mongo": {"enabled": True}})
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["doctor", "--config", str(cfg), "--skip-github"],
        )
        assert result.exit_code == 1

    def test_json_output_on_stdout(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        # CliRunner constructor changed between click 8.1 (mix_stderr kwarg)
        # and 8.2 (streams always separate). Construct without the kwarg and
        # parse the JSON block out of combined output — cheaper than a
        # version probe and works on both.
        try:
            runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
        except TypeError:
            runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["doctor", "--config", str(cfg), "--skip-github", "--json"],
        )
        assert result.exit_code == 0
        # The human table is emitted first (to stderr on 8.1 with
        # mix_stderr=False; to combined output on 8.2). The JSON payload
        # is the last contiguous JSON object in ``result.output``.
        combined = result.output
        first_brace = combined.find("\n{")
        if first_brace == -1 and combined.startswith("{"):
            first_brace = 0
        else:
            first_brace += 1  # skip the leading newline
        data = json.loads(combined[first_brace:])
        assert data["status"] in {"ok", "warn"}
        assert "checks" in data

    def test_strict_flag_is_propagated(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--strict upgrades WARN to FAIL for unreachable external services."""
        cfg = _write_config(tmp_path / "config.yml", {"mongo": {"enabled": True}})
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
        monkeypatch.setenv("MONGODB_URL", "mongodb://unreachable.invalid:27017")

        def _boom(_host: str, _port: int, *, timeout: float = 2.0) -> tuple[bool, str]:
            return False, "unreachable: test"

        monkeypatch.setattr("caretaker.doctor._check_tcp_reachable", _boom)

        runner = CliRunner()
        # Without --strict, mongo being unreachable is WARN → exit 0.
        result_warn = runner.invoke(
            cli_main,
            ["doctor", "--config", str(cfg), "--skip-github"],
        )
        assert result_warn.exit_code == 0
        # With --strict, mongo being unreachable is FAIL → exit 1.
        result_strict = runner.invoke(
            cli_main,
            ["doctor", "--config", str(cfg), "--skip-github", "--strict"],
        )
        assert result_strict.exit_code == 1


# ── Bootstrap-check ───────────────────────────────────────────────────
#
# The bootstrap preflight is the tight, offline subset that guards the
# pre-orchestrator failure modes we've actually seen in prod (bad
# workflow pin, unparseable config on the pinned tag, enabled agent
# whose secret was never provisioned). See the 2026-04-22 audio_engineer
# outage post-mortem for why the failure modes here matter.


class TestCheckImportOk:
    def test_import_check_passes_in_test_env(self) -> None:
        # If caretaker didn't import we wouldn't be running this test —
        # but the row still needs to exist so operators see confirmation
        # bootstrap-check itself ran.
        row = check_import_ok()
        assert row.severity is Severity.OK
        assert row.category == "bootstrap"


class TestCheckConfigParse:
    def test_missing_file_is_fail(self, tmp_path: pathlib.Path) -> None:
        row, loaded = check_config_parse(tmp_path / "does-not-exist.yml")
        assert row.severity is Severity.FAIL
        assert loaded is None

    def test_unparseable_yaml_is_fail(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "config.yml"
        bad.write_text(":\n  - this: is: not: valid: yaml\n    : :\n")
        row, loaded = check_config_parse(bad)
        assert row.severity is Severity.FAIL
        assert loaded is None

    def test_unknown_key_rejected_by_strict_model_is_fail(self, tmp_path: pathlib.Path) -> None:
        # StrictBaseModel uses ``extra="forbid"`` — an unknown top-level
        # key is exactly the kind of config drift bootstrap-check catches
        # when a repo's pin rolls past a schema change.
        bad = tmp_path / "config.yml"
        bad.write_text("version: v1\ntypo_key: whoops\n")
        row, loaded = check_config_parse(bad)
        assert row.severity is Severity.FAIL
        assert loaded is None

    def test_valid_config_returns_model(self, tmp_path: pathlib.Path) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        row, loaded = check_config_parse(cfg)
        assert row.severity is Severity.OK
        assert isinstance(loaded, MaintainerConfig)


class TestCheckVersionPin:
    def test_missing_file_is_fail(self, tmp_path: pathlib.Path) -> None:
        row = check_version_pin(tmp_path / "missing.version")
        assert row.severity is Severity.FAIL

    def test_empty_file_is_fail(self, tmp_path: pathlib.Path) -> None:
        pin = tmp_path / ".version"
        pin.write_text("")
        row = check_version_pin(pin)
        assert row.severity is Severity.FAIL

    def test_non_semver_is_fail(self, tmp_path: pathlib.Path) -> None:
        pin = tmp_path / ".version"
        pin.write_text("not-a-version\n")
        row = check_version_pin(pin)
        assert row.severity is Severity.FAIL

    def test_plain_semver_is_ok(self, tmp_path: pathlib.Path) -> None:
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        row = check_version_pin(pin)
        assert row.severity is Severity.OK
        assert "0.12.0" in row.detail

    def test_leading_v_accepted(self, tmp_path: pathlib.Path) -> None:
        pin = tmp_path / ".version"
        pin.write_text("v0.12.2\n")
        row = check_version_pin(pin)
        assert row.severity is Severity.OK


class TestCheckBootstrapEnvSecrets:
    def test_enabled_block_missing_env_is_fail(self) -> None:
        config = _load_config({"mongo": {"enabled": True}})
        rows = check_bootstrap_env_secrets(config, {"GITHUB_TOKEN": "x"})
        mongo_row = next(r for r in rows if r.name == "MONGODB_URL")
        assert mongo_row.severity is Severity.FAIL

    def test_disabled_block_emits_no_row(self) -> None:
        config = _load_config()  # mongo default = disabled
        rows = check_bootstrap_env_secrets(config, {"GITHUB_TOKEN": "x"})
        # Bootstrap mode skips disabled-block rows entirely — that's
        # the key difference from the full doctor's env check.
        assert not any(r.name == "MONGODB_URL" for r in rows)

    def test_missing_github_token_fails(self) -> None:
        config = _load_config()
        rows = check_bootstrap_env_secrets(config, {})
        token_row = next(r for r in rows if r.name == "GITHUB_TOKEN")
        assert token_row.severity is Severity.FAIL

    def test_present_anthropic_key_is_ok(self) -> None:
        config = _load_config()
        rows = check_bootstrap_env_secrets(
            config, {"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-x"}
        )
        ant = next(r for r in rows if r.name == "ANTHROPIC_API_KEY")
        assert ant.severity is Severity.OK


class TestRunBootstrapCheck:
    def test_happy_path(self, tmp_path: pathlib.Path) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        report = run_bootstrap_check(
            cfg,
            env={"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-x"},
            pin_path=pin,
        )
        assert not report.has_failures
        assert any(r.name == "import caretaker" for r in report.results)
        assert any(r.name == "config file" for r in report.results)
        assert any(r.name == "version pin" for r in report.results)

    def test_missing_pin_reports_fail(self, tmp_path: pathlib.Path) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        report = run_bootstrap_check(
            cfg,
            env={"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-x"},
            pin_path=tmp_path / "nope.version",
        )
        pin_row = next(r for r in report.results if r.name == "version pin")
        assert pin_row.severity is Severity.FAIL
        assert report.has_failures

    def test_bad_config_skips_env_checks(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "config.yml"
        bad.write_text("version: v1\nunknown: true\n")  # StrictBaseModel → fail
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        report = run_bootstrap_check(
            bad,
            env={"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-x"},
            pin_path=pin,
        )
        # Config FAIL short-circuits the env-var walk — there's no
        # MaintainerConfig to enumerate refs from. The single FAIL row
        # for the config is enough signal for the operator.
        assert report.has_failures
        assert not any(
            r.category == "bootstrap" and r.name == "MONGODB_URL" for r in report.results
        )

    def test_no_github_calls(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bootstrap-check must NEVER reach the network. We stub httpx's
        # client factory to raise so any attempted GitHub probe would
        # blow up the test.
        cfg = _write_config(tmp_path / "config.yml")
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise AssertionError("bootstrap-check must not make network calls")

        monkeypatch.setattr("httpx.AsyncClient", _boom)
        report = run_bootstrap_check(
            cfg,
            env={"GITHUB_TOKEN": "x", "ANTHROPIC_API_KEY": "sk-x"},
            pin_path=pin,
        )
        assert not report.has_failures


class TestBootstrapCheckCLI:
    def test_bootstrap_check_happy_path_exits_0(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "doctor",
                "--config",
                str(cfg),
                "--bootstrap-check",
                "--pin-path",
                str(pin),
            ],
        )
        assert result.exit_code == 0

    def test_bootstrap_check_reports_missing_config(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bootstrap-check surfaces a missing config file as a FAIL row
        # (exit 1), NOT an internal error (exit 2). That's the whole
        # point — operators need a clear actionable row.
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "doctor",
                "--config",
                str(tmp_path / "missing.yml"),
                "--bootstrap-check",
                "--pin-path",
                str(pin),
            ],
        )
        assert result.exit_code == 1
        assert (
            "config file" in result.output or "config file" in (result.stderr_bytes or b"").decode()
        )

    def test_bootstrap_check_json_on_stdout(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _write_config(tmp_path / "config.yml")
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        try:
            runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
        except TypeError:
            runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "doctor",
                "--config",
                str(cfg),
                "--bootstrap-check",
                "--pin-path",
                str(pin),
                "--json",
            ],
        )
        assert result.exit_code == 0
        combined = result.output
        first_brace = combined.find("\n{")
        if first_brace == -1 and combined.startswith("{"):
            first_brace = 0
        else:
            first_brace += 1
        data = json.loads(combined[first_brace:])
        assert data["status"] in {"ok", "warn"}
        assert any(c["category"] == "bootstrap" for c in data["checks"])

    def test_bootstrap_check_enabled_block_missing_secret_exits_1(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulates the audio_engineer-style failure: an enabled block
        # whose secret wasn't provisioned. Bootstrap-check must catch
        # it locally rather than deferring to a swallowed 403 at runtime.
        cfg = _write_config(
            tmp_path / "config.yml",
            {
                "fleet_registry": {
                    "enabled": True,
                    "oauth2": {"enabled": True},
                }
            },
        )
        pin = tmp_path / ".version"
        pin.write_text("0.12.0\n")
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        # Deliberately do NOT set OAUTH2_CLIENT_ID / OAUTH2_CLIENT_SECRET /
        # OAUTH2_TOKEN_URL — the enabled OAuth2 block demands them, so
        # bootstrap-check must FAIL.
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "doctor",
                "--config",
                str(cfg),
                "--bootstrap-check",
                "--pin-path",
                str(pin),
            ],
        )
        assert result.exit_code == 1

    def test_bootstrap_check_oauth2_only_passes_without_hmac_secret(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reproduces the space-tycoon v0.20.x failure mode: fleet_registry
        # is OAuth2-only and CARETAKER_FLEET_SECRET is intentionally unset
        # (HMAC was removed in v0.20.0). Bootstrap-check must NOT FAIL on
        # the absent legacy HMAC secret.
        cfg = _write_config(
            tmp_path / "config.yml",
            {
                "fleet_registry": {
                    "enabled": True,
                    "oauth2": {"enabled": True},
                }
            },
        )
        pin = tmp_path / ".version"
        pin.write_text("0.20.1\n")
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("OAUTH2_CLIENT_ID", "id")
        monkeypatch.setenv("OAUTH2_CLIENT_SECRET", "secret")
        monkeypatch.setenv("OAUTH2_TOKEN_URL", "https://oidc.example/token")
        # CARETAKER_FLEET_SECRET deliberately unset.
        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            [
                "doctor",
                "--config",
                str(cfg),
                "--bootstrap-check",
                "--pin-path",
                str(pin),
            ],
        )
        assert result.exit_code == 0, result.output
