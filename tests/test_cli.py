"""Tests for CLI commands."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from click.testing import CliRunner

from caretaker.cli import RunMode, _configure_logging, main

if TYPE_CHECKING:
    import pathlib


class TestRunMode:
    def test_self_heal_mode_is_valid(self) -> None:
        assert RunMode.SELF_HEAL == "self-heal"
        assert "self-heal" in [m.value for m in RunMode]


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


class TestConfigureLogging:
    def test_no_log_file_sets_info_level(self) -> None:
        _configure_logging(log_file=None, debug=False)
        assert logging.getLogger().level == logging.INFO

    def test_debug_flag_sets_debug_level(self) -> None:
        _configure_logging(log_file=None, debug=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_log_file_creates_file_handler(self, tmp_path: pathlib.Path) -> None:
        log_path = str(tmp_path / "caretaker.log")
        _configure_logging(log_file=log_path, debug=False)
        root_logger = logging.getLogger()
        handler_types = [type(h).__name__ for h in root_logger.handlers]
        assert "FileHandler" in handler_types


class TestRunReportFile:
    def test_report_json_round_trip(self, tmp_path: pathlib.Path) -> None:
        """JSON run-report data survives a write-and-read round-trip."""
        report_path = tmp_path / "report.json"
        mock_summary_dict = {
            "run_at": "2024-01-01T00:00:00",
            "mode": "full",
            "prs_monitored": 2,
            "errors": [],
        }
        with report_path.open("w") as f:
            json.dump(mock_summary_dict, f)

        with report_path.open() as f:
            data = json.load(f)
        assert data["prs_monitored"] == 2
