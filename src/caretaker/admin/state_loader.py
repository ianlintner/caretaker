"""Background refresh of orchestrator state for the admin dashboard.

State is persisted by the orchestrator as a hidden JSON block in the
``[Maintainer] Orchestrator State`` issue of each watched repo. The MCP
backend has no direct orchestrator handle (the orchestrator runs as a
GitHub Actions cron), so the dashboard hydrates its view by polling that
issue via the configured GitHub App installation.

Enabled when ``CARETAKER_ADMIN_WATCHED_REPO`` and the GitHub App
env vars (``CARETAKER_GITHUB_APP_ID``, ``CARETAKER_GITHUB_APP_PRIVATE_KEY``,
``CARETAKER_GITHUB_APP_INSTALLATION_ID``) are all set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from caretaker.github_app import (
    AppJWTSigner,
    GitHubAppCredentialsProvider,
    InstallationTokenMinter,
)
from caretaker.github_client.api import GitHubClient
from caretaker.state.tracker import StateTracker

if TYPE_CHECKING:
    from .data import AdminDataAccess

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60

# M6 fleet-tier :GlobalSkill promotion is an expensive pass (scans every
# :Skill node and runs the abstraction pass). Only run once per day so
# the admin loop doesn't re-redact on every 60-second tick. The 23h
# cutoff leaves a little slack around cron-adjacent schedules.
_PROMOTION_COOLDOWN = timedelta(hours=23)


def build_refresh_task(data: AdminDataAccess) -> asyncio.Task[None] | None:
    """Start the admin state refresh loop, or return ``None`` if unconfigured."""
    repo = os.environ.get("CARETAKER_ADMIN_WATCHED_REPO", "").strip()
    app_id_str = os.environ.get("CARETAKER_GITHUB_APP_ID", "").strip()
    private_key = os.environ.get("CARETAKER_GITHUB_APP_PRIVATE_KEY", "").strip()
    install_id_str = os.environ.get("CARETAKER_GITHUB_APP_INSTALLATION_ID", "").strip()

    if not (repo and app_id_str and private_key and install_id_str):
        logger.info(
            "Admin state refresh disabled: set CARETAKER_ADMIN_WATCHED_REPO plus "
            "CARETAKER_GITHUB_APP_ID / CARETAKER_GITHUB_APP_PRIVATE_KEY / "
            "CARETAKER_GITHUB_APP_INSTALLATION_ID to enable."
        )
        return None

    if "/" not in repo:
        logger.warning(
            "CARETAKER_ADMIN_WATCHED_REPO=%r is not in 'owner/repo' form; refresh disabled",
            repo,
        )
        return None

    try:
        app_id = int(app_id_str)
        installation_id = int(install_id_str)
    except ValueError:
        logger.error("GitHub App id or installation id is not an integer; refresh disabled")
        return None

    owner, name = repo.split("/", 1)

    try:
        interval = int(
            os.environ.get("CARETAKER_ADMIN_REFRESH_SECONDS", str(_DEFAULT_INTERVAL_SECONDS))
        )
    except ValueError:
        interval = _DEFAULT_INTERVAL_SECONDS
    interval = max(10, interval)

    signer = AppJWTSigner(app_id=app_id, private_key_pem=private_key)
    minter = InstallationTokenMinter(signer=signer)
    provider = GitHubAppCredentialsProvider(minter=minter, default_installation_id=installation_id)

    neo4j_url = os.environ.get("NEO4J_URL", "").strip()

    # Persistent graph store shared by the 60-second reconciliation pass
    # AND the process-wide ``GraphWriter`` (M1 of the memory-graph plan).
    # Holding one store across ticks avoids opening/closing a Neo4j driver
    # session on every refresh and lets agent call sites publish facts
    # directly without waiting for the next full_sync. Captured in a
    # nonlocal-assignable variable so the closure below can mutate it.
    persistent_store: Any = None
    writer_started = False
    # M4: track the wall-clock of the last nightly compaction pass so the
    # refresh loop only kicks it off once every 24h even though the loop
    # itself runs every minute. Stored as a monotonic timestamp so clock
    # skew on the host doesn't cause us to skip or double-fire.
    last_compaction_at: float | None = None
    compaction_interval_seconds = 24 * 60 * 60
    # M6 — tracks the last time :GlobalSkill promotion ran so we enforce
    # the 24h cooldown in the closure without reaching for a module-
    # level timestamp. ``None`` means "never run" which is always
    # eligible on the next tick.
    last_promotion_at: datetime | None = None

    async def _sync_graph(state) -> None:  # type: ignore[no-untyped-def]
        """Best-effort Neo4j sync. Swallows all errors — graph is optional."""
        nonlocal persistent_store, writer_started, last_promotion_at, last_compaction_at
        if not neo4j_url:
            return
        try:
            from caretaker.graph.builder import GraphBuilder
            from caretaker.graph.store import GraphStore
            from caretaker.graph.writer import get_writer

            if persistent_store is None:
                persistent_store = GraphStore()
                writer = get_writer()
                writer.configure(persistent_store)
                if not writer_started:
                    await writer.start()
                    writer_started = True

            counts = await GraphBuilder(persistent_store).full_sync(
                state,
                causal_store=data.causal_store,
                repo=f"{owner}/{name}",
            )
            logger.debug("Graph sync counts: %s", counts)
        except Exception:
            logger.warning("Graph sync failed", exc_info=True)

        # M6 fleet-tier sync runs after the main builder so every other
        # node (:Agent, goal:overall, per-repo :Skill rows) already
        # exists in the graph before we merge the RUNS_AGENT /
        # GOAL_HEALTH / SHARES_SKILL edges that point at them.
        try:
            from caretaker.fleet.graph import (
                promote_global_skills,
                sync_repos_to_graph,
            )
            from caretaker.fleet.store import get_store as _get_fleet_store

            if persistent_store is None:
                return

            fleet_counts = await sync_repos_to_graph(persistent_store, _get_fleet_store())
            logger.debug("Fleet graph sync counts: %s", fleet_counts)

            cfg = data.config
            share_skills = bool(cfg and cfg.fleet.share_skills)
            min_repos = cfg.fleet.min_repos_for_promotion if cfg else 3
            now = datetime.now(UTC)
            due = last_promotion_at is None or (now - last_promotion_at) >= _PROMOTION_COOLDOWN
            if share_skills and due:
                promoted = await promote_global_skills(
                    persistent_store,
                    data.insight_store,
                    min_repos=min_repos,
                    share_skills=True,
                )
                last_promotion_at = now
                if promoted:
                    logger.info(
                        "Fleet promotion: %d signatures promoted to :GlobalSkill",
                        len(promoted),
                    )
        except Exception:
            logger.warning("Fleet graph sync failed", exc_info=True)

    async def _maybe_run_compaction() -> None:
        """Fire :func:`compaction.run_nightly` at most once per 24h.

        Compaction is best-effort (``docs/memory-graph-plan.md`` §10):
        a Neo4j hiccup must never wedge the refresh loop, so every
        failure is logged and swallowed. The last-run timestamp is
        tracked in the closure scope rather than on the store to keep
        the scheduling state local to the refresh task.
        """
        nonlocal last_compaction_at
        if persistent_store is None:
            return
        now = time.monotonic()
        if last_compaction_at is not None and (
            now - last_compaction_at < compaction_interval_seconds
        ):
            return
        try:
            from caretaker.graph import compaction

            counts = await compaction.run_nightly(persistent_store, f"{owner}/{name}")
            logger.info("Nightly graph compaction for %s: %s", repo, counts)
        except Exception:
            logger.warning("Nightly graph compaction failed for %s", repo, exc_info=True)
        finally:
            # Mark the attempt regardless of outcome so a persistently
            # broken Neo4j doesn't turn the loop into a hot spin.
            last_compaction_at = now

    async def _loop() -> None:
        # WARNING level so the line is visible under uvicorn's default
        # root-logger config (which drops INFO from non-uvicorn loggers).
        logger.warning("Admin state refresh started (repo=%s, interval=%ds)", repo, interval)
        first = True
        while True:
            try:
                async with GitHubClient(credentials_provider=provider) as github:
                    state = await StateTracker(github, owner, name).load()
                    try:
                        await data.causal_store.refresh_from_github(github, owner, name, state)
                    except Exception:
                        logger.debug("Causal store refresh failed", exc_info=True)
                data.set_state(state)
                if first:
                    logger.warning(
                        "Admin state hydrated from %s: prs=%d issues=%d runs=%d",
                        repo,
                        len(state.tracked_prs),
                        len(state.tracked_issues),
                        len(state.run_history),
                    )
                    first = False
                else:
                    logger.debug(
                        "Admin state refreshed from %s: prs=%d issues=%d runs=%d",
                        repo,
                        len(state.tracked_prs),
                        len(state.tracked_issues),
                        len(state.run_history),
                    )
                await _sync_graph(state)
                await _maybe_run_compaction()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Admin state refresh failed for %s", repo, exc_info=True)
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="admin-state-refresh")


__all__ = ["build_refresh_task"]
