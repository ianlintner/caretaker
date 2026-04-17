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
    auto_approve_workflows: bool = False


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


class GoalEngineConfig(StrictBaseModel):
    enabled: bool = False
    goal_driven_dispatch: bool = False
    divergence_threshold: int = 3
    stale_threshold: int = 5
    max_history: int = 20


class ReviewAgentConfig(StrictBaseModel):
    enabled: bool = False
    mode: Literal["scheduled", "targeted"] = "scheduled"
    lookback_runs: int = 10
    lookback_days: int = 30
    artifact_dir: str = "artifacts/review"
    save_markdown: bool = True
    save_json: bool = True
    save_manifest: bool = True
    publish_summary_comments: bool = False
    comment_on_prs: bool = True
    comment_on_issues: bool = True
    minimum_comment_score: int = 0
    use_llm_for_retro: bool = True


class MemoryStoreConfig(StrictBaseModel):
    """Configuration for the disk-backed agent memory store."""

    enabled: bool = True
    # Path to the SQLite database file.  A relative path is resolved from the
    # current working directory (i.e. the GitHub Actions workspace root).
    db_path: str = ".caretaker-memory.db"
    # Write a JSON snapshot of the store to this path after every save so it
    # can be uploaded as a workflow artifact for auditing / rollback.
    snapshot_path: str = ".caretaker-memory-snapshot.json"
    # Hard cap on entries per namespace to prevent unbounded growth.
    max_entries_per_namespace: int = 1000


class AzureConfig(StrictBaseModel):
    """Configuration for Azure-specific integrations."""

    use_managed_identity: bool = False


class MCPConfig(StrictBaseModel):
    """Configuration for remote MCP servers."""

    enabled: bool = False
    endpoint: str | None = None
    auth_mode: Literal["none", "managed_identity", "token"] = "managed_identity"
    timeout_seconds: int = 30
    allowed_tools: list[str] = Field(default_factory=list)


class TelemetryConfig(StrictBaseModel):
    """Configuration for remote observability."""

    enabled: bool = False
    application_insights_connection_string_env: str = "APPLICATIONINSIGHTS_CONNECTION_STRING"


class GitHubAppConfig(StrictBaseModel):
    """Configuration for the optional GitHub App front-end.

    When ``enabled`` is ``False`` (the default) caretaker keeps its current
    ``GITHUB_TOKEN`` / ``COPILOT_PAT`` behavior unchanged.  When enabled, the
    orchestrator and the MCP backend can mint short-lived installation tokens
    and receive signed webhooks.

    See ``docs/github-app-plan.md`` for the full design.
    """

    enabled: bool = False
    # Numeric App ID registered on GitHub.  Kept as ``int | None`` so the
    # default configuration can omit it without the YAML round-trip failing.
    app_id: int | None = None
    # Name of the env var that holds the PEM-encoded private key.  The key
    # itself is never stored in config to keep it out of checked-in files.
    private_key_env: str = "CARETAKER_GITHUB_APP_PRIVATE_KEY"
    # Name of the env var that holds the webhook shared secret used for
    # ``X-Hub-Signature-256`` verification.
    webhook_secret_env: str = "CARETAKER_GITHUB_APP_WEBHOOK_SECRET"
    # Optional OAuth client id/secret env vars (only required when user-to-
    # server tokens are used for Copilot hand-off).
    oauth_client_id_env: str = "CARETAKER_GITHUB_APP_CLIENT_ID"
    oauth_client_secret_env: str = "CARETAKER_GITHUB_APP_CLIENT_SECRET"
    # Public base URL where the webhook receiver is reachable, for OAuth
    # redirects and install-flow links.
    public_base_url: str | None = None
    # Skew allowance (seconds) applied when refreshing installation tokens
    # before their 1h expiry.
    installation_token_refresh_skew_seconds: int = 300


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
    goal_engine: GoalEngineConfig = Field(default_factory=GoalEngineConfig)
    review_agent: ReviewAgentConfig = Field(default_factory=ReviewAgentConfig)
    memory_store: MemoryStoreConfig = Field(default_factory=MemoryStoreConfig)
    azure: AzureConfig = Field(default_factory=AzureConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    github_app: GitHubAppConfig = Field(default_factory=GitHubAppConfig)

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
