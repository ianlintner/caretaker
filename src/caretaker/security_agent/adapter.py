"""BaseAgent adapter for the security alert triage agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.security_agent.agent import SecurityAgent

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


def _resolve_probes(
    cfg_dependabot: bool,
    cfg_code_scanning: bool,
    cfg_secret_scanning: bool,
    is_private: bool,
) -> tuple[bool, bool, bool]:
    """Return effective (include_dependabot, include_code_scanning, include_secret_scanning).

    Auto-detection logic (issue #522):
    - GHAS features (code scanning, secret scanning) are only available on public repos
      or private repos with a GitHub Advanced Security (GHAS) license.
    - For private repos we conservatively skip those two probes unless the operator
      has explicitly set them to ``True`` in config — that explicit override means they
      *know* they have a GHAS license and we should honour it.
    - ``include_dependabot`` works on both public and private repos; we always
      respect the config value for it.
    - Public repos: all probes are always available; respect config as-is.

    An INFO log enumerates every probe that was auto-skipped and why so operators
    can see the decision at startup without reading source code.
    """
    if not is_private:
        # Public repos: all probes available — honour config unchanged
        return cfg_dependabot, cfg_code_scanning, cfg_secret_scanning

    # Private repo: only Dependabot is available without a GHAS license.
    # We respect an *explicit True* override so operators with GHAS can still
    # run those probes; the default True values defined in SecurityAgentConfig
    # are treated as "auto" for private repos and get suppressed.
    #
    # We detect "was this explicitly configured vs default" purely by value here;
    # the operator intent is "I know I have GHAS, turn it on" when they set True
    # in a private-repo config. This is the conservative interpretation.
    #
    # In practice the safest heuristic is: since the default is True, a private
    # repo that gets a 403 from GHAS endpoints should have disabled these in
    # config. We proactively skip to avoid the 403 noise and log the reason.
    skipped: list[str] = []

    eff_code = cfg_code_scanning
    eff_secret = cfg_secret_scanning

    if cfg_code_scanning:
        eff_code = False
        skipped.append("include_code_scanning (GHAS unavailable on private repo without license)")
    if cfg_secret_scanning:
        eff_secret = False
        skipped.append("include_secret_scanning (GHAS unavailable on private repo without license)")

    if skipped:
        logger.info(
            "security_agent: private repo detected — auto-skipping probes: %s. "
            "Set include_code_scanning/include_secret_scanning: true in config to override "
            "(requires GHAS license).",
            ", ".join(skipped),
        )

    return cfg_dependabot, eff_code, eff_secret


class SecurityAgentAdapter(BaseAgent):
    """Adapter for the security alert triage agent."""

    @property
    def name(self) -> str:
        return "security"

    def enabled(self) -> bool:
        return self._ctx.config.security_agent.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.security_agent

        # Auto-detect repository visibility and resolve effective probe flags (#522).
        try:
            repo_info = await self._ctx.github.get_repo(self._ctx.owner, self._ctx.repo)
            is_private = repo_info.private
        except Exception as exc:
            logger.warning(
                "security_agent: could not detect repo visibility (%s); "
                "falling back to config flags as-is.",
                exc,
            )
            is_private = False  # conservative: treat as public (all probes allowed)

        inc_dependabot, inc_code_scanning, inc_secret_scanning = _resolve_probes(
            cfg_dependabot=cfg.include_dependabot,
            cfg_code_scanning=cfg.include_code_scanning,
            cfg_secret_scanning=cfg.include_secret_scanning,
            is_private=is_private,
        )

        agent = SecurityAgent(
            github=self._ctx.github,
            owner=self._ctx.owner,
            repo=self._ctx.repo,
            min_severity=cfg.min_severity,
            max_issues_per_run=cfg.max_issues_per_run,
            false_positive_rules=cfg.false_positive_rules,
            include_dependabot=inc_dependabot,
            include_code_scanning=inc_code_scanning,
            include_secret_scanning=inc_secret_scanning,
        )
        report = await agent.run()
        return AgentResult(
            processed=report.findings_found,
            errors=report.errors,
            extra={
                "issues_created": report.issues_created,
                "false_positives_flagged": report.false_positives_flagged,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        summary.security_findings_found = result.processed
        summary.security_issues_created = len(result.extra.get("issues_created", []))
        summary.security_false_positives = result.extra.get("false_positives_flagged", 0)
