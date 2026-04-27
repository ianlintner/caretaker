"""Concrete AgentContextFactory for GitHub App webhook dispatch.

Wires together the installation token minter, GitHubClient construction,
per-repo MaintainerConfig fetching, and LLMRouter so the dispatcher
never needs to know about any of these.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import yaml

from caretaker.agent_protocol import AgentContext
from caretaker.config import MaintainerConfig
from caretaker.github_client.api import GitHubClient

if TYPE_CHECKING:
    from caretaker.github_app.installation_tokens import InstallationTokenMinter
    from caretaker.github_app.webhooks import ParsedWebhook
    from caretaker.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_CONFIG_PATH = ".github/maintainer/config.yml"


class GitHubAppContextFactory:
    """Build an :class:`AgentContext` for each incoming webhook delivery.

    One instance is created at backend startup and shared across all
    deliveries (it is stateless beyond the injected collaborators).

    Args:
        minter: The installation token minter. Must not be ``None`` — callers
            should verify the GitHub App is configured before constructing this.
        llm_router: Shared LLM router built from the backend's own config.
        default_config: Fallback ``MaintainerConfig`` used when the target
            repo has no ``.github/maintainer/config.yml``. Defaults to an
            all-defaults instance.
        dry_run: When ``True`` all constructed :class:`AgentContext` instances
            have ``dry_run=True`` so agents skip mutating API calls.
    """

    def __init__(
        self,
        *,
        minter: InstallationTokenMinter,
        llm_router: LLMRouter,
        default_config: MaintainerConfig | None = None,
        dry_run: bool = False,
    ) -> None:
        self._minter = minter
        self._llm_router = llm_router
        self._default_config = default_config or MaintainerConfig()
        self._dry_run = dry_run

    async def build(self, parsed: ParsedWebhook) -> AgentContext:
        """Mint a token, construct a client, and load the repo config."""
        if parsed.installation_id is None:
            raise ValueError(
                f"delivery {parsed.delivery_id}: installation_id is None — "
                "cannot mint installation token for anonymous deliveries"
            )

        token = await self._minter.get_token(parsed.installation_id)
        client = GitHubClient(token=token.token)

        owner, repo = _split_repo(parsed.repository_full_name, parsed.delivery_id)
        config = await self._load_config(client, owner, repo)

        logger.debug(
            "built AgentContext owner=%s repo=%s installation=%s delivery=%s",
            owner,
            repo,
            parsed.installation_id,
            parsed.delivery_id,
        )
        return AgentContext(
            github=client,
            owner=owner,
            repo=repo,
            config=config,
            llm_router=self._llm_router,
            dry_run=self._dry_run,
        )

    async def _load_config(self, client: GitHubClient, owner: str, repo: str) -> MaintainerConfig:
        """Fetch the repo's maintainer config or return the default."""
        try:
            raw = await client.get_file_contents(owner, repo, _CONFIG_PATH)
            if raw is None:
                logger.debug(
                    "no maintainer config at %s in %s/%s; using defaults",
                    _CONFIG_PATH,
                    owner,
                    repo,
                )
                return self._default_config

            content_b64: str = raw.get("content", "")
            content_bytes = base64.b64decode(content_b64.replace("\n", ""))
            data = yaml.safe_load(content_bytes.decode()) or {}
            return MaintainerConfig.model_validate(data)
        except Exception:
            logger.warning(
                "failed to load maintainer config from %s/%s; using defaults",
                owner,
                repo,
                exc_info=True,
            )
            return self._default_config


def _split_repo(repository_full_name: str | None, delivery_id: str) -> tuple[str, str]:
    """Split ``owner/repo`` into ``(owner, repo)``, raising on bad input."""
    if not repository_full_name or "/" not in repository_full_name:
        raise ValueError(
            f"delivery {delivery_id}: repository_full_name {repository_full_name!r} "
            "is not in 'owner/repo' format"
        )
    owner, _, repo = repository_full_name.partition("/")
    return owner, repo


__all__ = ["GitHubAppContextFactory"]
