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
from typing import TYPE_CHECKING

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

    async def _loop() -> None:
        logger.info("Admin state refresh started (repo=%s, interval=%ds)", repo, interval)
        while True:
            try:
                async with GitHubClient(credentials_provider=provider) as github:
                    state = await StateTracker(github, owner, name).load()
                data.set_state(state)
                logger.debug(
                    "Admin state refreshed from %s: prs=%d issues=%d runs=%d",
                    repo,
                    len(state.tracked_prs),
                    len(state.tracked_issues),
                    len(state.run_history),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Admin state refresh failed for %s", repo, exc_info=True)
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="admin-state-refresh")


__all__ = ["build_refresh_task"]
