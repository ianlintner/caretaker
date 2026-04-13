"""Tests for CLI commands."""

from __future__ import annotations

from click.testing import CliRunner

from caretaker.cli import main


class TestCLI:
    def test_validate_config_success(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("config.yml", "w") as f:
                f.write("version: v1\n")

            result = runner.invoke(main, ["validate-config", "--config", "config.yml"])

        assert result.exit_code == 0
        assert "Config valid" in result.output

    def test_validate_config_failure(self) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("config.yml", "w") as f:
                f.write("version: v2\n")

            result = runner.invoke(main, ["validate-config", "--config", "config.yml"])

        assert result.exit_code == 1
        assert "Invalid config" in result.output
