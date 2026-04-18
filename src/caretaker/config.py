"""Configuration models for caretaker."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

SUPPORTED_CONFIG_VERSIONS = {"v1"}


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnershipAutoClaimConfig(StrictBaseModel):
    """Configuration for which PR types Caretaker auto-claims ownership of."""

    copilot_prs: bool = True
    dependabot_prs: bool = True
    human_prs: bool = False


class OwnershipConfig(StrictBaseModel):
    """Configuration for PR ownership management."""

    enabled: bool = True
    auto_claim: OwnershipAutoClaimConfig = Field(default_factory=OwnershipAutoClaimConfig)
    label: str = "caretaker:owned"
    hold_label: str = "caretaker:hold"


class ReadinessConfig(StrictBaseModel):
    """Configuration for PR readiness evaluation."""

    enabled: bool = True
    check_name: str = "caretaker/pr-readiness"
    required_reviews: int = 1
    require_all_checks_passed: bool = True
    require_review_resolution: bool = True


class MergeAuthorityMode(StrEnum):
    """Merge authority modes for owned PRs.

    - advisory: Only publish readiness check, no merge authority
    - gate_only: Gate merge via required check, don't merge directly
    - gate_and_merge: Gate via required check AND merge directly when ready
    """

    ADVISORY = "advisory"
    GATE_ONLY = "gate_only"
    GATE_AND_MERGE = "gate_and_merge"


class MergeAuthorityConfig(StrictBaseModel):
    """Configuration for Caretaker merge authority."""

    mode: MergeAuthorityMode = MergeAuthorityMode.ADVISORY


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
    ownership: OwnershipConfig = Field(default_factory=OwnershipConfig)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)


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


DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5"
DEFAULT_REASONING_MODEL = "claude-opus-4-5"

DEFAULT_FEATURE_MODELS: dict[str, dict[str, int | str]] = {
    # Short classification/triage tasks — route to the faster/cheaper tier.
    "ci_log_analysis": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 2000},
    "analyze_review_comment": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 1000},
    "analyze_stuck_pr": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
    # Longer reasoning tasks — keep on the default (Sonnet) tier.
    "generate_reflection": {"model": DEFAULT_MODEL, "max_tokens": 1500},
    "generate_recovery_plan": {"model": DEFAULT_MODEL, "max_tokens": 2000},
    "decompose_issue": {"model": DEFAULT_MODEL, "max_tokens": 3000},
    # Deep reasoning tasks — route to Opus for complex analysis.
    "principal_architecture_review": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 4000},
    "principal_create_prd": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 6000},
    "principal_decompose_refactor": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 5000},
    "test_coverage_analysis": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 3000},
    "test_skeleton_generation": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 4000},
    "refactor_analysis": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 4000},
    "refactor_plan": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 3000},
    "perf_diff_analysis": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 3000},
    "migration_analysis": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 4000},
    "migration_plan": {"model": DEFAULT_REASONING_MODEL, "max_tokens": 5000},
}


class FeatureModelConfig(StrictBaseModel):
    """Per-feature model override."""

    model: str | None = None
    max_tokens: int | None = None


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
    # Provider selection: "anthropic" (default, direct SDK) or "litellm"
    # (multi-provider: OpenAI, Vertex, Azure OpenAI, Azure AI Foundry,
    # Bedrock, Ollama, Mistral, Cohere, Groq, etc.)
    provider: Literal["anthropic", "litellm"] = "anthropic"
    # Model used when a feature has no explicit override. For litellm this
    # can be prefixed (e.g. "openai/gpt-4o", "azure_ai/gpt-4o", "vertex_ai/gemini-1.5-pro").
    default_model: str = DEFAULT_MODEL
    # Per-request timeout in seconds.
    timeout_seconds: float = 60.0
    # Per-feature model/max_tokens overrides — deep-merged on top of DEFAULT_FEATURE_MODELS.
    feature_models: dict[str, FeatureModelConfig] = Field(default_factory=dict)
    # Fallback model chain — only used when provider="litellm".  Each entry is
    # a LiteLLM-format model string tried in order if the primary call fails.
    fallback_models: list[str] = Field(default_factory=list)


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


class EvolutionConfig(StrictBaseModel):
    """Configuration for the learn-and-adapt evolution layer."""

    enabled: bool = False
    # Storage backend: "sqlite" (default, zero-dependency) or "mongo"
    # (requires mongo.enabled=true and MONGODB_URL env var).
    backend: Literal["sqlite", "mongo"] = "sqlite"
    db_path: str = ".caretaker-evolution.db"
    skill_min_confidence: float = 0.5
    reflection_enabled: bool = True
    mutation_enabled: bool = False  # opt-in; requires review of mutation outcomes
    plan_mode_enabled: bool = False  # opt-in; creates GitHub milestones + issues


class PrincipalAgentConfig(StrictBaseModel):
    """Configuration for the principal/lead engineer agent.

    Performs architecture reviews, PRD generation, and refactor decomposition
    using Opus-class models for deep reasoning.
    """

    enabled: bool = False
    auto_review_large_prs: bool = True
    large_pr_threshold: int = 300
    prd_labels: list[str] = Field(default_factory=lambda: ["needs-prd", "architecture"])
    model_override: str | None = None


class TestAgentConfig(StrictBaseModel):
    """Configuration for the test coverage and quality agent."""

    enabled: bool = False
    coverage_threshold: float = 0.8
    detect_flaky: bool = True
    generate_skeletons: bool = True
    max_skeletons_per_run: int = 3


class RefactorAgentConfig(StrictBaseModel):
    """Configuration for the code smell detection and refactoring agent."""

    enabled: bool = False
    auto_create_prs: bool = False
    max_prs_per_run: int = 1
    min_confidence: float = 0.8
    target_patterns: list[str] = Field(
        default_factory=lambda: ["dead_code", "duplication", "long_function"]
    )


class PerformanceAgentConfig(StrictBaseModel):
    """Configuration for the performance anti-pattern detection agent."""

    enabled: bool = False
    benchmark_job_name: str | None = None
    regression_threshold_pct: float = 10.0
    anti_patterns: list[str] = Field(
        default_factory=lambda: ["n_plus_one", "unbounded_loop", "missing_pagination"]
    )


class MigrationAgentConfig(StrictBaseModel):
    """Configuration for the framework/language migration agent."""

    enabled: bool = False
    target_migrations: list[dict[str, str]] = Field(default_factory=list)
    auto_fix_simple: bool = False
    max_fixes_per_run: int = 5


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
    # Storage backend: "sqlite" (default, zero-dependency) or "mongo" (Phase 1, requires
    # MongoConfig.enabled=true and MONGODB_URL env var set).
    backend: Literal["sqlite", "mongo"] = "sqlite"
    # Path to the SQLite database file.  A relative path is resolved from the
    # current working directory (i.e. the GitHub Actions workspace root).
    # Ignored when backend="mongo".
    db_path: str = ".caretaker-memory.db"
    # Write a JSON snapshot of the store to this path after every save so it
    # can be uploaded as a workflow artifact for auditing / rollback.
    snapshot_path: str = ".caretaker-memory-snapshot.json"
    # Hard cap on entries per namespace to prevent unbounded growth.
    max_entries_per_namespace: int = 1000


class AzureConfig(StrictBaseModel):
    """Configuration for Azure-specific integrations."""

    use_managed_identity: bool = False


class MongoConfig(StrictBaseModel):
    """Phase 1 — MongoDB / Cosmos DB for MongoDB durable state backend.

    Use a free SaaS MongoDB:
    - **Azure Cosmos DB for MongoDB** (https://azure.microsoft.com/free) —
      always-free tier: 1,000 RU/s + 25 GB; no credit card required.
    - **MongoDB Atlas** (https://www.mongodb.com/atlas) — M0 free cluster.

    Set the connection URL via the env var named in ``mongodb_url_env``.

    Example .caretaker.yml::

        mongo:
          enabled: true
          mongodb_url_env: MONGODB_URL   # set in GitHub Actions / .env
    """

    enabled: bool = False
    # Name of the env var holding a standard MongoDB connection URI.
    # Works with Cosmos DB for MongoDB, Atlas, or local mongod.
    # e.g. mongodb+srv://user:pass@cluster.cosmos.azure.com/?tls=true
    mongodb_url_env: str = "MONGODB_URL"
    # MongoDB database name.
    database_name: str = "caretaker"
    # Collection name for the agent memory store.
    memory_collection: str = "agent_memory"
    # Collection name for the audit log.
    audit_collection: str = "audit_log"
    # Evolution layer collections (used when evolution.backend = "mongo")
    evolution_skills_collection: str = "evolution_skills"
    evolution_mutations_collection: str = "evolution_mutations"


class RedisConfig(StrictBaseModel):
    """Phase 1 — Redis cache / dedup backend.

    Use a free SaaS Redis (e.g. Upstash https://upstash.com, Redis Cloud free).
    Set the connection URL via the env var named in ``redis_url_env``.

    Upstash free tier: 10 K commands/day, 256 MB — plenty for webhook dedup
    and installation-token caching at hobby / small-team scale.

    Example .caretaker.yml::

        redis:
          enabled: true
          redis_url_env: REDIS_URL   # set in GitHub Actions / .env
    """

    enabled: bool = False
    # Name of the env var holding a standard Redis URL.
    # Works with Upstash, Redis Cloud, Railway, or local Redis.
    # e.g. rediss://default:pass@host:port
    redis_url_env: str = "REDIS_URL"
    # TTL (seconds) for webhook delivery-id dedup keys.
    dedup_ttl_seconds: int = 3600
    # TTL (seconds) for cached GitHub App installation tokens (< 3600 s expiry).
    token_cache_ttl_seconds: int = 3000


class AuditLogConfig(StrictBaseModel):
    """Phase 1 — structured audit-log writer.

    Writes one document per agent decision to the MongoDB ``audit_log``
    collection when MongoDB is enabled.  When MongoDB is disabled, audit
    entries are emitted as structured log lines only.
    """

    enabled: bool = True


class MCPConfig(StrictBaseModel):
    """Configuration for remote MCP servers."""

    enabled: bool = False
    endpoint: str | None = None
    auth_mode: Literal["none", "managed_identity", "token", "apim"] = "managed_identity"
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


class FoundryExecutorConfig(StrictBaseModel):
    """Settings for the Foundry (Azure AI Foundry / LiteLLM) coding executor.

    Disabled by default.  When enabled, the ``ExecutorDispatcher`` routes
    eligible tasks through the in-process executor instead of dispatching to
    Copilot via comment markers.
    """

    enabled: bool = False
    # LiteLLM-format model string (e.g. "azure_ai/gpt-4o", "openai/gpt-4o").
    model: str = "azure_ai/gpt-4o"
    fallback_models: list[str] = Field(default_factory=list)
    max_iterations: int = 20
    max_tokens_per_task: int = 200_000
    workspace_timeout_seconds: int = 600
    allowed_commands: list[str] = Field(
        default_factory=lambda: ["ruff", "black", "isort", "prettier", "eslint"]
    )
    write_denylist: list[str] = Field(
        default_factory=lambda: [
            ".github/workflows/**",
            ".github/agents/**",
            ".caretaker.yml",
            ".github/maintainer/**",
            "scripts/release*",
            "setup.py",
        ]
    )
    max_files_touched: int = 10
    max_diff_lines: int = 400
    # MVP task types that are actually dispatched to Foundry today. ``UPGRADE``
    # is intentionally omitted until ``UpgradePlanner`` routes via the
    # dispatcher; users who wire an upstream-Copilot upgrade path to Foundry
    # can opt in by overriding this list in their config.
    allowed_task_types: list[str] = Field(
        default_factory=lambda: ["LINT_FAILURE", "REVIEW_COMMENT"]
    )
    route_same_repo_only: bool = True
    request_timeout_seconds: float = 120.0


class ExecutorConfig(StrictBaseModel):
    """Top-level switch deciding how coding tasks are executed."""

    provider: Literal["copilot", "foundry", "auto"] = "copilot"
    foundry: FoundryExecutorConfig = Field(default_factory=FoundryExecutorConfig)


class MaintainerConfig(StrictBaseModel):
    version: Literal["v1"] = "v1"
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    # Optional foundry/copilot executor routing. Omit (or leave at default
    # provider=copilot) to preserve legacy behavior byte-identically.
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
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
    principal_agent: PrincipalAgentConfig = Field(default_factory=PrincipalAgentConfig)
    test_agent: TestAgentConfig = Field(default_factory=TestAgentConfig)
    refactor_agent: RefactorAgentConfig = Field(default_factory=RefactorAgentConfig)
    perf_agent: PerformanceAgentConfig = Field(default_factory=PerformanceAgentConfig)
    migration_agent: MigrationAgentConfig = Field(default_factory=MigrationAgentConfig)
    memory_store: MemoryStoreConfig = Field(default_factory=MemoryStoreConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    azure: AzureConfig = Field(default_factory=AzureConfig)
    mongo: MongoConfig = Field(default_factory=MongoConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    audit_log: AuditLogConfig = Field(default_factory=AuditLogConfig)
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
