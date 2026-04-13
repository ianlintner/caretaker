"""CLI entrypoint for project-maintainer."""

from __future__ import annotations

import asyncio
import json
import sys
from enum import Enum

import click

from project_maintainer.orchestrator import Orchestrator


class RunMode(str, Enum):
    FULL = "full"
    PR_ONLY = "pr-only"
    ISSUE_ONLY = "issue-only"
    UPGRADE_ONLY = "upgrade"
    DRY_RUN = "dry-run"
    EVENT = "event"


@click.group()
@click.version_option()
def main() -> None:
    """Project Maintainer — autonomous repo management."""


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
def run(
    config: str,
    mode: str,
    event_type: str | None,
    event_payload: str | None,
    dry_run: bool,
) -> None:
    """Run the maintainer orchestrator."""
    parsed_mode = RunMode(mode)
    if dry_run:
        parsed_mode = RunMode.DRY_RUN

    payload = None
    if event_payload:
        payload = json.loads(event_payload)

    orchestrator = Orchestrator.from_config_path(config)
    exit_code = asyncio.run(
        orchestrator.run(mode=parsed_mode, event_type=event_type, event_payload=payload)
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
