"""CLI entrypoint for caretaker."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from enum import StrEnum

import click

from caretaker.config import MaintainerConfig
from caretaker.orchestrator import Orchestrator


class RunMode(StrEnum):
    FULL = "full"
    PR_ONLY = "pr-only"
    ISSUE_ONLY = "issue-only"
    UPGRADE_ONLY = "upgrade"
    SELF_HEAL = "self-heal"
    DRY_RUN = "dry-run"
    EVENT = "event"


def _configure_logging(log_file: str | None, debug: bool) -> None:
    """Configure root logger with console and optional file handlers."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # always capture debug in the file
        file_handler.setFormatter(logging.Formatter(fmt))
        handlers.append(file_handler)
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


@click.group()
@click.version_option()
def main() -> None:
    """Caretaker — autonomous repo management."""


@main.command()
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to config.yml")
@click.option(
    "--mode",
    type=click.Choice([m.value for m in RunMode]),
    default=RunMode.FULL.value,
    help="Run mode",
)
@click.option("--event-type", default=None, help="GitHub event type (push, pull_request, etc.)")
@click.option("--event-payload", default=None, help="JSON-encoded GitHub event payload")
@click.option("--dry-run", is_flag=True, default=False, help="Read-only mode")
@click.option(
    "--log-file",
    default=None,
    envvar="CARETAKER_LOG_FILE",
    help="Write full DEBUG-level log to this file (uploaded as a CI artifact).",
)
@click.option(
    "--report-file",
    default=None,
    envvar="CARETAKER_REPORT_FILE",
    help="Write a JSON run-report to this file (uploaded as a CI artifact).",
)
@click.option("--debug", is_flag=True, default=False, help="Enable DEBUG logging to stderr.")
def run(
    config: str,
    mode: str,
    event_type: str | None,
    event_payload: str | None,
    dry_run: bool,
    log_file: str | None,
    report_file: str | None,
    debug: bool,
) -> None:
    """Run the maintainer orchestrator."""
    _configure_logging(log_file, debug)

    parsed_mode = RunMode(mode)
    if dry_run:
        parsed_mode = RunMode.DRY_RUN

    payload = None
    if event_payload:
        payload = json.loads(event_payload)

    orchestrator = Orchestrator.from_config_path(config)
    exit_code = asyncio.run(
        orchestrator.run(
            mode=parsed_mode,
            event_type=event_type,
            event_payload=payload,
            report_path=report_file,
        )
    )
    sys.exit(exit_code)


@main.command("validate-config")
@click.option("--config", required=True, type=click.Path(exists=True), help="Path to config.yml")
def validate_config(config: str) -> None:
    """Validate maintainer config file and exit."""
    try:
        loaded = MaintainerConfig.from_yaml(config)
    except Exception as exc:  # pragma: no cover - surfaced to CLI user
        click.echo(f"❌ Invalid config: {exc}", err=True)
        raise SystemExit(1) from exc

    click.echo(
        f"✅ Config valid (version={loaded.version}, schedule={loaded.orchestrator.schedule})"
    )


if __name__ == "__main__":
    main()
