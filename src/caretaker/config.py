"""Configuration models for caretaker."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from caretaker.guardrails.policy import GuardrailsConfig, MergeRollbackConfig

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
    # NOTE: auto_approve_copilot is currently a no-op — accepted for backward
    # compatibility with existing configs but not yet wired into the review
    # flow. Tracked as a follow-up to Sprint 2 E2 (auto-merge after Copilot
    # post-approval push), where the same review-state semantics need to be
    # decided. Setting this to true today has no effect.
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
    # When a PR has been open this long without progressing to merge-ready and
    # without a human review approval, escalate it to a human. Catches the
    # long-tail abandonment cases (portfolio #4 was open 10 days; #28 was
    # open 7 days) that the within-cycle stuck-detection doesn't see.
    # 0 disables the gate.
    stuck_age_hours: int = 24
    # Post-merge rollback window (Agentic Design Patterns Ch. 18 Checkpoint
    # & Rollback). Disabled by default on first ship — operators promote
    # per-repo once they are comfortable with the 5-minute CI-watch
    # after each merge. When enabled, :func:`caretaker.pr_agent.merge.perform_merge`
    # wraps the merge API call in :func:`caretaker.guardrails.checkpoint_and_rollback`
    # and reverts the merge if base-branch CI flips red inside the window.
    merge_rollback: MergeRollbackConfig = Field(default_factory=MergeRollbackConfig)


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


class TriageConfig(StrictBaseModel):
    """Unified triage for PRs + issues + cross-entity cascade cleanup.

    See memory/project_pr_triage.md for the motivating behavior.
    """

    enabled: bool = True
    pr_triage: bool = True
    issue_triage: bool = True
    cascade: bool = True
    # Paths whose sole presence makes a PR diff "empty" (close candidate).
    # Binary state files committed by bots end up here; see 2026-04-21 cleanup.
    binary_only_paths: list[str] = Field(default_factory=lambda: [".caretaker-memory.db"])
    # When true, triage produces a report but takes no destructive action.
    dry_run: bool = False
    # Stale cutoff for issues marked with no activity, in days.
    stale_issue_days: int = 30


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
    "ci_triage": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
    "analyze_review_comment": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 1000},
    "review_classification": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
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


class AgenticBotIdentityConfig(StrictBaseModel):
    """Tunables for :mod:`caretaker.identity`'s LLM fallback path.

    When ``llm_lookup_enabled`` is False (default) the classifier never calls
    the LLM — it behaves identically to the synchronous deterministic
    allowlist. Enable only once the deterministic coverage has been audited.
    """

    llm_lookup_enabled: bool = False
    llm_ttl_seconds: int = 86_400
    llm_cache_max_size: int = 1_000


class LLMConfig(StrictBaseModel):
    # Allow population by either the new canonical name ``llm_enabled`` or the
    # legacy name ``claude_enabled`` so existing configs keep working. We
    # override the StrictBaseModel ``extra`` policy here only for the alias;
    # unknown keys still raise per StrictBaseModel's default.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Master switch for the ENTIRE LLM router (not just the Claude/Anthropic
    # provider). When set to ``"false"`` the router hard-disables regardless
    # of which provider is selected — including LiteLLM / Azure AI / OpenAI /
    # Vertex — and every LLM-dependent feature falls back to its non-LLM path.
    #
    # Values:
    #   - ``"auto"``   (default) – activate if any provider credentials are found.
    #   - ``"true"``   – force-activate and WARN if credentials are missing.
    #   - ``"false"``  – hard-disable the router (all providers).
    #
    # The legacy field name ``claude_enabled`` is accepted as an alias for
    # backwards compatibility and will be removed in a future major release.
    # See ``docs/configuration.md`` for migration guidance.
    llm_enabled: Literal["auto", "true", "false"] = Field(
        default="auto",
        validation_alias=AliasChoices("llm_enabled", "claude_enabled"),
        serialization_alias="llm_enabled",
        description=(
            "Master switch for the LLM router. 'false' disables ALL providers "
            "(including LiteLLM / Azure AI / OpenAI), not just Claude. "
            "Alias: claude_enabled (deprecated)."
        ),
    )
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
    # Number of retries for ``ClaudeClient.structured_complete`` when the model
    # returns malformed JSON or a payload that fails pydantic validation.
    # Set to 0 to disable the self-correcting retry loop.
    structured_output_retries: int = 1
    # Tunables for :mod:`caretaker.identity`'s optional LLM fallback when
    # classifying an unfamiliar login. Temporarily nested under ``LLMConfig``
    # until a dedicated ``AgenticConfig`` lands (T-D1); may be promoted
    # without a breaking change because callers read through this model.
    bot_identity: AgenticBotIdentityConfig = Field(default_factory=AgenticBotIdentityConfig)


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


class FixLadderConfig(StrictBaseModel):
    """Deterministic-first fix ladder (Wave A3).

    Runs a small, ordered set of signature-gated rungs (ruff-format,
    ruff-check-fix, mypy-install-types, pip-compile-upgrade,
    pytest-lastfail) against a working-tree sandbox before the
    self-heal agent escalates to the LLM path. Each rung is a
    short-lived subprocess with bounded stdout/stderr capture — see
    :mod:`caretaker.self_heal_agent.sandbox` for the runner.

    The pattern follows the BitsAI-Fix / Factory.ai / KubeIntellect
    research: the deterministic ladder catches the 80% of failures
    that are formatter-style churn without burning tokens on a full
    LLM fix cycle. The escalation path is only invoked when the
    ladder produces partial or no progress, and the escalation prompt
    now carries the list of rungs already tried so the LLM doesn't
    re-suggest them.

    Defaults to ``enabled=False`` because the ladder opens PRs
    autonomously — operators should promote it per-repo once they've
    reviewed the default rung set.
    """

    # Master switch. Default off so existing installs keep the
    # legacy LLM-escalation-only flow until explicitly opted in.
    enabled: bool = False
    # Upper bound on how many rungs one dispatch may execute. Shields
    # operators from a misconfigured ladder burning CI minutes.
    max_rungs_per_incident: int = 6
    # Branch name prefix for auto-opened fix PRs. The full branch is
    # ``<prefix>/<error-sig>``.
    branch_prefix: str = "caretaker/fix-ladder"
    # Label applied to fix-ladder PRs so operators can filter them.
    pr_label: str = "caretaker:fix-ladder"


class SelfHealAgentConfig(StrictBaseModel):
    enabled: bool = True
    # Whether to report bugs / feature requests to the upstream caretaker repo
    report_upstream: bool = True
    # Suppress upstream reporting if this repo IS the upstream (set true for ianlintner/caretaker)
    is_upstream_repo: bool = False
    # Cooldown (hours) before creating another issue for the same job+kind
    cooldown_hours: int = 6
    # Deterministic-first fix ladder (Wave A3). When ``enabled`` the
    # self-heal agent runs the ladder before the LLM escalation path
    # fires; ladder outcomes of ``fixed`` / ``partial`` short-circuit
    # the escalation, ``escalated`` feeds the ladder context forward
    # into the LLM prompt, and ``no_op`` falls through unchanged.
    fix_ladder: FixLadderConfig = Field(default_factory=FixLadderConfig)


class SecurityAgentConfig(StrictBaseModel):
    enabled: bool = True
    min_severity: str = "medium"
    max_issues_per_run: int = 5
    false_positive_rules: list[str] = Field(default_factory=list)
    include_dependabot: bool = True
    include_code_scanning: bool = True
    include_secret_scanning: bool = True


class DependencyBisectorConfig(StrictBaseModel):
    """Configuration for the grouped-Dependabot PR bisector."""

    enabled: bool = False
    max_runs: int = 6
    # Label that marks PRs Caretaker has claimed. The bisector only
    # fires on grouped PRs that carry this label to avoid acting on
    # third-party-owned PRs.
    owned_label: str = "caretaker:owned"


class DependencyAgentConfig(StrictBaseModel):
    enabled: bool = True
    auto_merge_patch: bool = True
    auto_merge_minor: bool = True
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    post_digest: bool = True
    bisector: DependencyBisectorConfig = Field(default_factory=DependencyBisectorConfig)


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


class PRReviewerConfig(StrictBaseModel):
    """Configuration for the dual-path PR code reviewer.

    When ``enabled`` is ``True``, caretaker reviews opened/updated PRs:
    - Low-complexity PRs (score < ``routing_threshold``) get an inline
      LLM review posted as a GitHub pull-request review.
    - High-complexity PRs get handed off to the ``claude-code-action``
      workflow via a trigger label + structured ``@claude`` comment.

    Set ``enabled = false`` to disable. By default the agent also runs on
    polling-only deployments (``webhook_only = false``) so it works out of
    the box without a webhook bridge. The ``skip_labels`` guard prevents
    re-review loops.
    """

    enabled: bool = True
    # When True, skip the polling fallback — only act on webhook-delivered
    # pull_request events. Default is False so that polling-only deployments
    # (the common case for GitHub Actions cron triggers) still run pr_reviewer.
    # Set to True only if you have a webhook dispatcher wired up AND want to
    # minimise GitHub REST calls.
    webhook_only: bool = False
    # PR actions that trigger a review. Defaults include ready_for_review so
    # Copilot-bot drafts (which always open as drafts and are flipped to
    # ready later) are caught, and synchronize/reopened so force-pushed
    # revisions get re-reviewed (paired with ``skip_labels`` for idempotency).
    trigger_actions: list[str] = Field(
        default_factory=lambda: [
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
        ]
    )
    # Score threshold: score >= threshold → claude-code hand-off; else inline LLM.
    routing_threshold: int = 40
    # Label/mention used for the claude-code-action hand-off.
    claude_code_label: str = "claude-code"
    claude_code_mention: str = "@claude"
    # Maximum diff lines fetched for inline review (excess is truncated).
    max_diff_lines: int = 2000
    # Whether to post per-file inline comments (in addition to the review body).
    post_inline_comments: bool = True
    # Skip PRs marked as draft.
    skip_draft: bool = True
    # Skip PRs that already carry any of these labels (prevents re-review).
    skip_labels: list[str] = Field(default_factory=lambda: ["caretaker:reviewed"])
    # Review event forced on all inline reviews — "AUTO" lets the LLM decide.
    review_event: Literal["AUTO", "COMMENT", "APPROVE", "REQUEST_CHANGES"] = "AUTO"


class PRCIApproverConfig(StrictBaseModel):
    """Configuration for the ``pr_ci_approver`` agent.

    Closes the operational gap where GitHub Actions workflow runs
    triggered by bot accounts (Copilot, dependabot, github-actions[bot])
    land with ``conclusion=action_required`` and require manual owner
    approval via the Actions UI. With no intervention these runs sit
    forever, and caretaker's ``pr_reviewer`` → merge loop silently
    stalls because PRs never go green. See ``docs/qa-findings-2026-04-23.md``
    finding #7 for the motivating scenario.

    Default behaviour is **surface-only** (``auto_approve = false``):
    we detect stuck runs and escalate them into the digest so an
    operator can approve with one click. Enable ``auto_approve = true``
    only after you've verified your ``allowed_actors`` list is tight.
    """

    enabled: bool = True
    # Bot actors whose runs are considered safe to surface/approve.
    # Exact-match against the run's ``actor.login`` and ``triggering_actor.login``.
    # Keep this list tight: adding a general account here is equivalent to
    # giving that account bypass-on-first-party-code rights.
    allowed_actors: list[str] = Field(
        default_factory=lambda: [
            "Copilot",
            "copilot-swe-agent[bot]",
            "github-actions[bot]",
            "dependabot[bot]",
            "the-care-taker[bot]",
        ]
    )
    # When True, call the approve endpoint. When False (default) the agent
    # only *surfaces* stuck runs in the digest and as a maintainer:escalated
    # issue hint — no side effects on GitHub.
    auto_approve: bool = False
    # Maximum runs to process per caretaker run (cap API usage).
    max_runs_per_run: int = 25
    # Skip runs older than this many hours (avoids approving ancient runs
    # that have been superseded by later pushes).
    max_age_hours: int = 48
    # Only act on runs whose event is in this set. ``pull_request`` is the
    # common case; ``issue_comment`` covers @copilot nudges that re-trigger
    # workflows.
    trigger_events: list[str] = Field(
        default_factory=lambda: ["pull_request", "issue_comment"]
    )


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
    # Opt-in switch for the T-E2 cross-run memory retriever (see
    # ``caretaker.memory.retriever``). When true, the Phase 2 LLM decision
    # call sites (starting with PR readiness) inject up to three prior
    # :class:`AgentCoreMemory` snapshots into their prompts and the
    # :mod:`caretaker.memory.core` write path computes + stores a
    # ``summary_embedding`` on every dispatch when an embedder is wired.
    # Defaults to false so existing installs don't start embedding without
    # explicit opt-in — see ``docs/plans/2026-Q2-agentic-migration.md`` T-E2.
    retrieval_enabled: bool = False
    # Wave A3 write-path toggle. When true (or ``retrieval_enabled`` is
    # true) the :mod:`caretaker.memory.core` publisher and the self-heal
    # ``:Incident`` writer compute + store a ``summary_embedding`` when
    # an embedder is configured. Split from ``retrieval_enabled`` so
    # operators can seed the corpus (Wave B3 needs it) before flipping
    # the reader on — the writer is cheap, the reader touches every
    # LLM prompt. Fail-closed: no embedder → no embedding stored.
    write_embeddings: bool = False


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


class AdminDashboardConfig(StrictBaseModel):
    """Configuration for the admin dashboard.

    Uses OIDC (OpenID Connect) for authentication via an external provider
    (e.g. rust-oauth2-server).  Sessions are stored in Redis.
    """

    enabled: bool = False
    # OIDC discovery URL — e.g. https://auth.example.com/.well-known/openid-configuration
    oidc_issuer_url: str = ""
    oidc_client_id_env: str = "CARETAKER_ADMIN_OIDC_CLIENT_ID"
    oidc_client_secret_env: str = "CARETAKER_ADMIN_OIDC_CLIENT_SECRET"
    # Session lifetime in seconds.
    session_ttl_seconds: int = 3600
    # Optional email allowlist.  When non-empty, only these emails may log in.
    allowed_emails: list[str] = Field(default_factory=list)
    # CORS origins allowed for the admin API (dev convenience).
    cors_origins: list[str] = Field(default_factory=list)
    # Secret key for signing session cookies.  Read from this env var.
    session_secret_env: str = "CARETAKER_ADMIN_SESSION_SECRET"
    # Public base URL for OAuth redirect callbacks.
    public_base_url: str = ""


class AttributionConfig(StrictBaseModel):
    """Attribution telemetry knobs (R&D workstream A2).

    The telemetry fields themselves (``caretaker_touched`` / ``merged`` /
    ``operator_intervened`` on :class:`~caretaker.state.models.TrackedPR`
    and :class:`~caretaker.state.models.TrackedIssue`) round-trip through
    Pydantic JSON with defaults, so existing Mongo/SQLite rows load cleanly
    without a destructive schema migration. What this knob governs is
    *when* those defaults get materialised back into the persisted state:

    * ``lazy`` (default) — populate on next write. Existing rows keep
      their missing fields until the next run causes ``save()`` to serialise
      them fresh. Zero runtime cost; the only caveat is that the weekly
      attribution rollup will count under-reported values for repos that
      haven't run since the feature shipped.
    * ``eager`` — the orchestrator runs a one-pass
      :func:`caretaker.state.intervention_detector.backfill_missing_fields`
      on load() so every tracked row has the attribution fields set before
      the first action of the run. Costs one extra pass over the tracked
      state; useful for operators who want the weekly dashboard accurate
      from the first run post-upgrade.

    Lazy is the safe default: the worst case is a few days of partial
    attribution data for repos that run infrequently. Eager is the
    preferred mode for high-value repos where the dashboard needs to be
    correct immediately.
    """

    migration_strategy: Literal["eager", "lazy"] = "lazy"


class GraphStoreConfig(StrictBaseModel):
    """Configuration for the Neo4j graph store."""

    enabled: bool = False
    neo4j_url_env: str = "NEO4J_URL"
    neo4j_auth_env: str = "NEO4J_AUTH"
    database: str = "caretaker"


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


class OAuth2ClientConfig(StrictBaseModel):
    """OAuth2 ``client_credentials`` settings for service-to-service auth.

    Opt-in: when ``enabled`` is ``True`` AND all three env vars named by
    ``client_id_env`` / ``client_secret_env`` / ``token_url_env`` are
    populated, consumers like :class:`FleetRegistryConfig` will attach a
    bearer token to outbound requests instead of (or alongside) HMAC.

    The default names match the conventional ``OAUTH2_CLIENT_ID`` /
    ``OAUTH2_CLIENT_SECRET`` / ``OAUTH2_TOKEN_URL`` triple that caretaker
    writes into consumer repos when an operator provisions client
    credentials against a shared authorization server.
    """

    enabled: bool = False
    client_id_env: str = "OAUTH2_CLIENT_ID"
    client_secret_env: str = "OAUTH2_CLIENT_SECRET"
    token_url_env: str = "OAUTH2_TOKEN_URL"
    scope_env: str = "OAUTH2_SCOPE"
    # Requested scope if ``scope_env`` is not populated. Empty string means
    # "use the server-side default scope set for this client".
    default_scope: str = ""
    timeout_seconds: float = 10.0


class FleetRegistryConfig(StrictBaseModel):
    """Opt-in fleet registry.

    When ``enabled`` is ``True`` and ``endpoint`` is set, each successful
    orchestrator run POSTs a small JSON heartbeat to a central caretaker
    backend so an operator can see every consumer repo in one dashboard.

    The feature is entirely opt-in: the default ``enabled = False`` keeps
    caretaker's current behavior byte-identical. The endpoint URL is
    intentionally not given a default — caretaker never phones home
    unless the consumer explicitly configures a destination.

    ``secret_env`` names an environment variable whose value is used as
    an HMAC-SHA256 shared secret. When set, the emitter signs the POST
    body and forwards the hex digest in ``X-Caretaker-Signature``; the
    backend verifies before recording. When unset, heartbeats are
    delivered without authentication (suitable for private networks /
    trusted origins only).

    ``oauth2`` is an alternative (or additional) auth mode: when its
    ``enabled`` flag is True and its env vars are populated, the emitter
    fetches a bearer token via the OAuth2 ``client_credentials`` grant
    and sends it in the ``Authorization`` header. HMAC + OAuth2 may be
    used together; the backend can require either or both.
    """

    enabled: bool = False
    endpoint: str | None = None
    secret_env: str = "CARETAKER_FLEET_SECRET"
    timeout_seconds: float = 5.0
    # When ``True`` the heartbeat body includes the full ``RunSummary``
    # dump; when ``False`` only the curated set of summary counters is
    # sent. Default False to minimise the risk of surfacing repo-private
    # details (error log snippets, etc.) through the central dashboard.
    include_full_summary: bool = False
    oauth2: OAuth2ClientConfig = Field(default_factory=OAuth2ClientConfig)


class FleetAlertConfig(StrictBaseModel):
    """T-E4 — server-side :FleetAlert evaluator.

    Attached to :class:`FleetConfig` (inbound / backend-owned fleet state)
    and gated behind ``enabled = False`` by default so existing installs
    see byte-identical behaviour. The evaluator is pure Python; the only
    observable side effects when enabled are the in-memory alert store
    populated by the admin endpoint and the ``:FleetAlert`` graph nodes
    upserted via :func:`caretaker.fleet.alerts.upsert_fleet_alerts`.
    """

    enabled: bool = False
    goal_health_threshold: float = 0.7
    goal_health_n_consecutive: int = 3
    error_spike_multiplier: float = 3.0
    ghosted_window_days: int = 7


class FleetConfig(StrictBaseModel):
    """M6 — fleet-tier graph + :GlobalSkill promotion.

    Distinct from :class:`FleetRegistryConfig`, which governs the outbound
    heartbeat emitter: this block governs the inbound / server-side
    behaviour of the fleet graph. The default keeps every knob off so
    existing installs see byte-identical behaviour.

    * ``share_skills`` is the master switch for cross-repo skill
      promotion. When ``False`` (the default), ``promote_global_skills``
      is a no-op even if ``min_repos_for_promotion`` is met — privacy
      over ergonomics.
    * ``min_repos_for_promotion`` is the gate on how many distinct
      ``repo`` values a ``:Skill`` signature must appear in before it
      is eligible for the two-gate promotion (the other gate being the
      abstraction pass in ``caretaker.fleet.abstraction``).
    * ``include_global_in_prompts`` closes the read-loop on promotion
      (T-E3). When ``True`` (the default), ``InsightStore.get_relevant``
      returns the union of local ``:Skill`` hits and fleet-promoted
      ``:GlobalSkill`` hits so the prompt builder can surface
      cross-repo skills with a ``[fleet]`` prefix. Operators can flip
      this off per-repo if a shared skill misfires — promotion itself
      is unaffected.
    * ``alerts`` is the :FleetAlert evaluator surface (T-E4). See
      :class:`FleetAlertConfig`.
    """

    share_skills: bool = False
    min_repos_for_promotion: int = 3
    include_global_in_prompts: bool = True
    alerts: FleetAlertConfig = Field(default_factory=FleetAlertConfig)


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
    # Task types dispatched to the custom executor. Expanded from the
    # original MVP pair (``LINT_FAILURE``, ``REVIEW_COMMENT``) to include
    # ``TEST_FAILURE`` — trivial test failures (assertion tweak, fixture
    # rename) fit inside the same size budget as lint fixes and the
    # executor's tool-loop already handles them.
    #
    # Still intentionally omitted:
    # * ``UPGRADE``     — waits on ``UpgradePlanner`` wiring the dispatcher.
    # * ``CI_FAILURE``  — too ambiguous; let Copilot take it until we have a
    #                     classifier that routes only trivial CI breaks here.
    # * ``BUILD_FAILURE`` — usually dependency / env issues outside the
    #                     executor's write-denylist.
    # * ``REFACTOR``, ``MIGRATION``, ``ARCHITECTURE_REVIEW``, ``PRD_GENERATION``
    #                   — bigger than the size budget by definition.
    allowed_task_types: list[str] = Field(
        default_factory=lambda: [
            "LINT_FAILURE",
            "REVIEW_COMMENT",
            "TEST_FAILURE",
        ]
    )
    route_same_repo_only: bool = True
    request_timeout_seconds: float = 120.0


class ClaudeCodeExecutorConfig(StrictBaseModel):
    """Configuration for the opt-in ``claude-code-action`` hand-off executor.

    Caretaker does not run Claude Code inline; instead, when this executor
    is selected for a task, it applies a configurable *trigger label* to
    the host PR / issue, and posts a structured hand-off comment. The
    upstream [``anthropics/claude-code-action``][cca] workflow, installed
    separately in the consumer repo, listens for that label (or the `@claude`
    mention in the comment) and produces the fix asynchronously.

    The caretaker state machine then tracks the resulting commit / PR
    through the same ``<!-- caretaker:result -->`` markers it already uses
    for the Copilot + Foundry paths.

    Feature is entirely opt-in: ``enabled = False`` by default; in addition
    the consumer repo must have the upstream action installed and
    authorised on its own.

    [cca]: https://github.com/anthropics/claude-code-action
    """

    enabled: bool = False
    # Label caretaker applies to trigger the upstream workflow.
    trigger_label: str = "claude-code"
    # Mention string included in the hand-off comment so the upstream
    # auto-detector can pick it up even if a repo has a different label
    # listener name configured.
    mention: str = "@claude"
    # Maximum attempts per task before caretaker stops re-applying the
    # trigger label; prevents ping-pong if the upstream action can't
    # complete the work.
    max_attempts: int = 2


class K8sAgentWorkerConfig(StrictBaseModel):
    """On-demand Kubernetes Job worker for the custom coding agent.

    Opt-in Phase 3 rollout surface from
    ``docs/custom-coding-agent-plan.md``. When enabled on the caretaker
    backend, the admin API exposes ``POST /api/admin/agent-tasks``; each
    call spawns a short-lived ``batch/v1 Job`` that runs the custom
    executor against a single issue / PR. Uses the template + RBAC from
    ``infra/k8s/caretaker-agent-worker.yaml``.

    Consumers' own maintainer workflows do NOT invoke this path — they
    continue to run the executor inline. This is an operator-facing
    dispatch channel used by the admin dashboard / UI.
    """

    enabled: bool = False
    namespace: str = "caretaker"
    image: str | None = None
    service_account: str = "caretaker-agent-worker"
    # Name of the template Job we clone per dispatch. Matches the
    # ``metadata.name`` in ``infra/k8s/caretaker-agent-worker.yaml``.
    template_job_name: str = "caretaker-agent-worker-template"
    # Generated Job names become ``{name_prefix}-{slug}-{short-sha}``.
    name_prefix: str = "caretaker-agent"
    # Redis-backed dedupe — an identical (repo, issue_number) dispatch
    # within this window returns the existing Job name instead of
    # creating a new pod. Set to 0 to disable dedupe.
    dedupe_ttl_seconds: int = 900
    # Mirrors the manifest defaults; overridable per-deployment.
    ttl_seconds_after_finished: int = 600
    active_deadline_seconds: int = 900


class ExecutorConfig(StrictBaseModel):
    """Top-level switch deciding how coding tasks are executed."""

    provider: Literal["copilot", "foundry", "claude_code", "auto"] = "copilot"
    foundry: FoundryExecutorConfig = Field(default_factory=FoundryExecutorConfig)
    claude_code: ClaudeCodeExecutorConfig = Field(default_factory=ClaudeCodeExecutorConfig)
    k8s_worker: K8sAgentWorkerConfig = Field(default_factory=K8sAgentWorkerConfig)


class AgenticEnforceGateConfig(StrictBaseModel):
    """Gate that blocks ``shadow → enforce`` flips below an agreement floor.

    Consumed by :mod:`caretaker.eval.gate` and the ``enforce-gate``
    GitHub Actions workflow. The threshold is inclusive — a site whose
    7-day rolling agreement rate equals the floor is allowed to flip.
    """

    min_agreement_rate: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum 7-day rolling agreement rate (across all per-site scorers) "
            "required before a PR is allowed to flip ``mode`` from ``shadow`` to "
            "``enforce`` for this site. Checked by the enforce-gate CI workflow "
            "against the most recent :mod:`caretaker.eval.store` report."
        ),
    )


class AgenticDomainConfig(StrictBaseModel):
    """Per-decision-site knobs for the Phase 2 agentic migration.

    The ``mode`` field is the three-way switch consumed by
    :func:`caretaker.evolution.shadow.shadow_decision`:

    * ``off`` — classic heuristic is authoritative; LLM path never runs.
    * ``shadow`` — both paths run, legacy verdict returned, disagreements
      logged.
    * ``enforce`` — LLM candidate is authoritative, legacy is the
      fall-through safety net.

    Additional per-domain knobs (thresholds, sampling, per-feature model
    overrides) can be added here later without breaking callers; every
    decision site gets its own :class:`AgenticDomainConfig` instance on
    :class:`AgenticConfig` so the knobs fan out cleanly.
    """

    mode: Literal["off", "shadow", "enforce"] = "off"
    enforce_gate: AgenticEnforceGateConfig = Field(default_factory=AgenticEnforceGateConfig)
    # Optional per-site model override. When set, the candidate leg of
    # @shadow_decision uses this model instead of llm.default_model, enabling
    # A/B comparison of two models against the legacy heuristic via the
    # nightly-eval harness. Example: set to "azure_ai/claude-sonnet-4" while
    # the legacy leg (and LLM calls outside shadow decisions) continues to
    # use llm.default_model. Leave None to inherit.
    model_override: str | None = None
    # Optional per-site max-tokens override; only consumed when model_override is set.
    max_tokens_override: int | None = None


class IssueTriageAgenticConfig(AgenticDomainConfig):
    """Per-decision knobs for the issue-triage shadow migration (T-A5).

    Extends :class:`AgenticDomainConfig` with the candidate-pool sizing knob
    the migration plan calls out. When the LLM candidate runs, the caller
    pre-selects at most ``dup_candidate_pool_size`` nearby open issues via
    embedding similarity (not yet wired) or keyword Jaccard overlap, and
    passes them into the structured-complete prompt so the model can cite
    a concrete duplicate_of number instead of inventing one.
    """

    dup_candidate_pool_size: int = Field(
        default=5,
        ge=0,
        le=50,
        description=(
            "Maximum number of nearby open issues to pre-select as duplicate "
            "candidates. 0 disables candidate pre-selection (LLM must judge "
            "duplicate_of from title alone, which typically means it returns "
            "null). Capped at 50 to bound prompt size."
        ),
    )


class AgenticConfig(StrictBaseModel):
    """Flags for the Phase 2 LLM decision migrations.

    Every field defaults to ``mode="off"`` so classic heuristics stay
    authoritative until operators explicitly opt in. The full list
    matches §3 of the 2026-Q2 agentic migration plan.
    """

    readiness: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    ci_triage: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    review_classification: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    issue_triage: IssueTriageAgenticConfig = Field(default_factory=IssueTriageAgenticConfig)
    cascade: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    stuck_pr: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    bot_identity: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    dispatch_guard: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    executor_routing: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    crystallizer_category: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)


class MaintainerConfig(StrictBaseModel):
    version: Literal["v1"] = "v1"
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    # Optional foundry/copilot executor routing. Omit (or leave at default
    # provider=copilot) to preserve legacy behavior byte-identically.
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    pr_agent: PRAgentConfig = Field(default_factory=PRAgentConfig)
    issue_agent: IssueAgentConfig = Field(default_factory=IssueAgentConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
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
    pr_reviewer: PRReviewerConfig = Field(default_factory=PRReviewerConfig)
    pr_ci_approver: PRCIApproverConfig = Field(default_factory=PRCIApproverConfig)
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
    fleet_registry: FleetRegistryConfig = Field(default_factory=FleetRegistryConfig)
    fleet: FleetConfig = Field(default_factory=FleetConfig)
    github_app: GitHubAppConfig = Field(default_factory=GitHubAppConfig)
    admin_dashboard: AdminDashboardConfig = Field(default_factory=AdminDashboardConfig)
    graph_store: GraphStoreConfig = Field(default_factory=GraphStoreConfig)
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    attribution: AttributionConfig = Field(default_factory=AttributionConfig)
    # Unified guardrails (Agentic Design Patterns Ch. 18): sanitize_input
    # on every external-input boundary, filter_output on every outbound
    # GitHub write, checkpoint_and_rollback on post-merge state mutations.
    # Enabled by default — this is safety, not a feature.
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)

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
