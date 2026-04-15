"""Configuration models for caretaker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

SUPPORTED_CONFIG_VERSIONS = {"v1"}


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutoMergeConfig(StrictBaseModel):
    copilot_prs: bool = True
    dependabot_prs: bool = True
    human_prs: bool = False
    merge_method: Literal["squash", "merge", "rebase"] = "squash"


class CopilotConfig(StrictBaseModel):
    max_retries: int = 2
    retry_window_hours: int = 24
    context_injection: bool = True


class CIConfig(StrictBaseModel):
    flaky_retries: int = 1
    ignore_jobs: list[str] = Field(default_factory=list)
    close_managed_prs_on_backlog: bool = False


class ReviewConfig(StrictBaseModel):
    auto_approve_copilot: bool = False
    nitpick_threshold: Literal["low", "high"] = "low"


class PRAgentConfig(StrictBaseModel):
    enabled: bool = True
    auto_merge: AutoMergeConfig = Field(default_factory=AutoMergeConfig)
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)


class IssueAgentLabels(StrictBaseModel):
    bug: list[str] = Field(default_factory=lambda: ["bug"])
    feature: list[str] = Field(default_factory=lambda: ["enhancement", "feature"])
    question: list[str] = Field(default_factory=lambda: ["question"])


class IssueAgentConfig(StrictBaseModel):
    enabled: bool = True
    auto_assign_bugs: bool = True
    auto_assign_features: bool = False
    auto_close_stale_days: int = 30
    auto_close_questions: bool = True
    labels: IssueAgentLabels = Field(default_factory=IssueAgentLabels)


class UpgradeAgentConfig(StrictBaseModel):
    enabled: bool = True
    strategy: Literal["auto-minor", "auto-patch", "latest", "pinned", "manual"] = "auto-minor"
    channel: Literal["stable", "preview"] = "stable"
    auto_merge_non_breaking: bool = True


class EscalationConfig(StrictBaseModel):
    targets: list[str] = Field(default_factory=list)
    stale_days: int = 7
    labels: list[str] = Field(default_factory=lambda: ["maintainer:escalated"])


class LLMConfig(StrictBaseModel):
    claude_enabled: Literal["auto", "true", "false"] = "auto"
    claude_features: list[str] = Field(
        default_factory=lambda: [
            "ci_log_analysis",
            "architectural_review",
            "issue_decomposition",
            "upgrade_impact_analysis",
        ]
    )


class OrchestratorConfig(StrictBaseModel):
    schedule: Literal["hourly", "daily", "weekly", "manual"] = "daily"
    summary_issue: bool = True
    dry_run: bool = False


class DevOpsAgentConfig(StrictBaseModel):
    enabled: bool = True
    # Branch to monitor for CI failures
    target_branch: str = "main"
    # Maximum fix-issues opened per caretaker run (avoid spam on persistent failures)
    max_issues_per_run: int = 3
    # Re-open or skip if a similar open issue already exists
    dedup_open_issues: bool = True
    # Cooldown (hours) before creating another issue for the same job+category
    cooldown_hours: int = 6


class SelfHealAgentConfig(StrictBaseModel):
    enabled: bool = True
    # Whether to report bugs / feature requests to the upstream caretaker repo
    report_upstream: bool = True
    # Suppress upstream reporting if this repo IS the upstream (set true for ianlintner/caretaker)
    is_upstream_repo: bool = False
    # Cooldown (hours) before creating another issue for the same job+kind
    cooldown_hours: int = 6


class SecurityAgentConfig(StrictBaseModel):
    enabled: bool = True
    min_severity: str = "medium"
    max_issues_per_run: int = 5
    false_positive_rules: list[str] = Field(default_factory=list)
    include_dependabot: bool = True
    include_code_scanning: bool = True
    include_secret_scanning: bool = True


class DependencyAgentConfig(StrictBaseModel):
    enabled: bool = True
    auto_merge_patch: bool = True
    auto_merge_minor: bool = True
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    post_digest: bool = True


class DocsAgentConfig(StrictBaseModel):
    enabled: bool = True
    lookback_days: int = 7
    changelog_path: str = "CHANGELOG.md"
    update_readme: bool = False


class StaleAgentConfig(StrictBaseModel):
    enabled: bool = True
    stale_days: int = 60
    close_after: int = 14
    close_stale_prs: bool = True
    delete_merged_branches: bool = True
    exempt_labels: list[str] = Field(default_factory=list)


class CharlieAgentConfig(StrictBaseModel):
    enabled: bool = True
    stale_days: int = 14
    close_duplicate_issues: bool = True
    close_duplicate_prs: bool = True
    close_stale_issues: bool = True
    close_stale_prs: bool = True
    exempt_labels: list[str] = Field(default_factory=list)


class HumanEscalationConfig(StrictBaseModel):
    enabled: bool = True
    post_digest_issue: bool = True
    notify_assignees: list[str] = Field(default_factory=list)


class MaintainerConfig(StrictBaseModel):
    version: Literal["v1"] = "v1"
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    pr_agent: PRAgentConfig = Field(default_factory=PRAgentConfig)
    issue_agent: IssueAgentConfig = Field(default_factory=IssueAgentConfig)
    upgrade_agent: UpgradeAgentConfig = Field(default_factory=UpgradeAgentConfig)
    devops_agent: DevOpsAgentConfig = Field(default_factory=DevOpsAgentConfig)
    self_heal_agent: SelfHealAgentConfig = Field(default_factory=SelfHealAgentConfig)
    security_agent: SecurityAgentConfig = Field(default_factory=SecurityAgentConfig)
    dependency_agent: DependencyAgentConfig = Field(default_factory=DependencyAgentConfig)
    docs_agent: DocsAgentConfig = Field(default_factory=DocsAgentConfig)
    charlie_agent: CharlieAgentConfig = Field(default_factory=CharlieAgentConfig)
    stale_agent: StaleAgentConfig = Field(default_factory=StaleAgentConfig)
    human_escalation: HumanEscalationConfig = Field(default_factory=HumanEscalationConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> MaintainerConfig:
        with open(path) as f:
            loaded = yaml.safe_load(f)

        if loaded is None:
            data: dict[str, Any] = {}
        elif not isinstance(loaded, dict):
            raise ValueError("Config YAML root must be a mapping/object")
        else:
            data = loaded

        version = data.get("version", "v1")
        if version not in SUPPORTED_CONFIG_VERSIONS:
            supported = ", ".join(sorted(SUPPORTED_CONFIG_VERSIONS))
            raise ValueError(
                f"Unsupported config version '{version}'. Supported versions: {supported}"
            )

        return cls.model_validate(data)
