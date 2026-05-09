"""Configuration models for caretaker."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

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
    caretaker_prs: bool = True
    maintainer_bot_prs: bool = True
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
    # Names of CheckRun jobs whose ``conclusion=success`` count as a bot
    # approval for the "Required reviews satisfied" gate. The default lists
    # caretaker's own ``claude-review`` job — which posts its review as a
    # CheckRun, not a formal Reviews API submission, so without this gate
    # the readiness comment would forever read "required_review_missing"
    # even after the bot signed off (PR #609 was the motivating incident).
    bot_check_names: list[str] = Field(default_factory=lambda: ["claude-review"])
    # Substrings (case-insensitive) that, when found in a bot-authored review
    # body or PR issue-comment body, count the comment as an explicit approval.
    # Used in addition to ``bot_check_names`` so a Claude reply that says
    # "Approved" or "LGTM" in plain English also satisfies the review gate.
    bot_approval_markers: list[str] = Field(
        default_factory=lambda: ["**approved**", "lgtm", "✅ approved", "approved by"]
    )
    # CheckRun names produced by caretaker's own supervisor workflow
    # (``maintainer.yml`` jobs) that should be excluded from the upstream-CI
    # rollup. Including them would make caretaker self-gate: when caretaker
    # itself is the running CI, "ci_pending" stays true forever. Always
    # ignored regardless of ``ci.ignore_jobs``.
    caretaker_workflow_check_names: list[str] = Field(
        default_factory=lambda: ["doctor", "maintain", "self-heal-on-failure"]
    )
    # Polling cadence used by the long-running ``resync_open_prs`` loop in
    # the GitHub App webhook server (and any future agent-worker loop). The
    # GitHub Actions cron path is independent and lives in the workflow
    # file. Set to 0 to disable in-process polling and rely solely on
    # webhooks + cron.
    resync_interval_seconds: int = 60


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
    caretaker_prs: bool = True
    maintainer_bot_prs: bool = True
    human_prs: bool = False
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    # Label that opts any individual PR into auto-merge regardless of its
    # author type. Applied automatically when a human posts "@caretaker merge"
    # on the PR, or can be added manually. Overrides human_prs=false.
    merge_opt_in_label: str = "caretaker:merge"


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
    # Deprecated no-op — accepted for backward compat with existing consumer
    # configs but never wired into the review flow. Will be removed in the
    # next major version; do not add new references to this field.
    auto_approve_copilot: bool = False
    nitpick_threshold: Literal["low", "high"] = "low"
    # When CI is green and there are no blocking review findings on a caretaker
    # PR (claude/ or caretaker/ branch), automatically submit an APPROVE review
    # so the repo's required-review gate is satisfied without human intervention.
    #
    # Defaults to False (staged rollout — see PR #579 review notes). Operators
    # opt in per-repo once they're comfortable with the auto-approve flow and
    # the head-SHA idempotency gate in :meth:`_handle_review_approve`.
    auto_approve_caretaker_prs: bool = False
    # When CI is green and a human has opted a PR into auto-merge via
    # ``@caretaker merge`` or the ``caretaker:merge`` label, automatically
    # submit an APPROVE review so the required-review gate is satisfied.
    # Like auto_approve_caretaker_prs this defaults to False — operators
    # opt in once they trust the merge command flow end-to-end.
    auto_approve_opted_in_prs: bool = False
    # When a reviewer signals infeasibility (duplicate, won't work, out of
    # scope), close the PR instead of dispatching a fix — prevents wasting
    # Copilot/Claude Code cycles on hopeless tasks.
    #
    # Defaults to False (staged rollout — substring matching on review body
    # has high false-positive risk; see PR #579 review notes). Opt in per-repo
    # once the verdict heuristic has been hardened or replaced by a structured
    # decision channel.
    close_on_infeasible_review: bool = False
    # PR additions threshold above which a blocker review triggers escalation
    # to a human instead of a mechanical fix attempt.
    high_loc_escalate_threshold: int = 500


class PRAgentConfig(StrictBaseModel):
    enabled: bool = True
    auto_merge: AutoMergeConfig = Field(default_factory=AutoMergeConfig)
    copilot: CopilotConfig = Field(default_factory=CopilotConfig)
    ci: CIConfig = Field(default_factory=CIConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    ownership: OwnershipConfig = Field(default_factory=OwnershipConfig)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    # Controls whether caretaker/pr-readiness is advisory (informational only,
    # never blocks branch protection) or acts as a required gate.  Defaults to
    # advisory so operators can add the check to the GitHub UI without it
    # accidentally blocking PRs.  Set to gate_only or gate_and_merge only when
    # you have deliberately configured branch protection to require this check.
    merge_authority: MergeAuthorityConfig = Field(default_factory=MergeAuthorityConfig)
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
    # When True, new issues are parked in TRIAGED on the first agent cycle
    # (with a triage-summary comment and the caretaker:triaged label) and
    # only dispatched to a coding agent on a subsequent cycle. This prevents
    # duplicate/spam issues from being immediately assigned before dedup
    # checks have run. Defaults to False for backward compatibility —
    # operators opt in per-repo via .github/maintainer/config.yml.
    triage_gate: bool = False


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


class ShepherdConfig(StrictBaseModel):
    """Shepherd mode — codify the manual PR cleanup loop as a routine pass.

    Phases (run in order when enabled):
      1. Inventory  — list open PRs, enrich with mergeStateStatus via GraphQL.
      2. Dedupe     — reuse ``close_duplicate_fix_prs`` (CVE/pkg grouping).
      3. Promote    — reuse ``ready_valid_copilot_drafts`` to flip green drafts
                      ready-for-review (CI-green only).
      4. Mechanical — Delta C: run ruff/line-wrap/F401 fixers on lint-failing PRs.
      5. Rebase     — Delta C: ``update_branch_if_behind`` for BEHIND PRs.
      6. Reap       — Delta C: close DIRTY drafts older than ``stale_dirty_days``.
      7. Merge chain — Delta C/D: squash in dependency order w/ update between.
      8. LLM escalate — Delta F: metered ``stuck_pr_llm`` for stuck PRs.

    Disabled by default; opt-in via ``shepherd.enabled: true`` in config.yml.
    Dry-run inherits from ``orchestrator.dry_run`` unless overridden.
    """

    enabled: bool = False
    # Phase toggles — let operators stage rollout instead of all-or-nothing.
    dedupe: bool = True
    promote_drafts: bool = True
    mechanical_fixes: bool = True
    auto_update_branch: bool = True
    stale_dirty_reaper: bool = True
    merge_chain: bool = True
    # Per-run LLM call budget (Delta F). 0 disables LLM escalation entirely.
    max_llm_calls_per_run: int = Field(default=3, ge=0)
    # Age in days before a DIRTY draft is closed as stale. Must be >= 1 so
    # a misconfigured 0 doesn't close every DIRTY draft on first run.
    stale_dirty_days: int = Field(default=14, ge=1)
    # Which mechanical fixers to try, in order. Names map to fix_ladder rungs.
    mechanical_fix_rungs: list[str] = Field(
        default_factory=lambda: ["ruff-format", "ruff-check-fix"],
    )
    # When true, shepherd writes report but takes no destructive action.
    # Falls back to ``orchestrator.dry_run`` when unset at runtime.
    dry_run: bool = False


class UpgradeAgentConfig(StrictBaseModel):
    enabled: bool = True
    strategy: Literal["auto-minor", "auto-patch", "latest", "pinned", "manual"] = "auto-minor"
    channel: Literal["stable", "preview"] = "stable"
    auto_merge_non_breaking: bool = True
    # When True, the upgrade-agent will mark its own draft PRs ready-for-review
    # once all required CI checks pass.  This closes the loop where Copilot opens
    # upgrade PRs as drafts and they never auto-promote because ``pr_ci_approver``
    # isn't enabled on the consumer repo.
    auto_ready_drafts: bool = True


class EscalationConfig(StrictBaseModel):
    targets: list[str] = Field(default_factory=list)
    stale_days: int = 7
    labels: list[str] = Field(default_factory=lambda: ["maintainer:escalated"])


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-6"
# Architecture-level reasoning uses Sonnet 4.6 rather than Opus —
# Opus is ~5x the cost of Sonnet for marginal quality gain on the
# refactor/PRD/migration tasks we use it for. Operators who want
# Opus back can override per-feature via ``feature_models``.
DEFAULT_REASONING_MODEL = "claude-sonnet-4-6"

DEFAULT_FEATURE_MODELS: dict[str, dict[str, int | str]] = {
    # Short classification/triage tasks — route to the faster/cheaper tier.
    "ci_log_analysis": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 2000},
    "ci_triage": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
    "analyze_review_comment": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 1000},
    "review_classification": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
    "analyze_stuck_pr": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 800},
    # PR complexity tier classifier — runs once per caretaker-owned PR to
    # pick which model the review/fix should use. Must stay cheap; Haiku
    # is plenty for a 4-way classification with structured output.
    "complexity_classifier": {"model": DEFAULT_TRIAGE_MODEL, "max_tokens": 300},
    # Longer reasoning tasks — keep on the default (Sonnet) tier.
    "generate_reflection": {"model": DEFAULT_MODEL, "max_tokens": 1500},
    "generate_recovery_plan": {"model": DEFAULT_MODEL, "max_tokens": 2000},
    "decompose_issue": {"model": DEFAULT_MODEL, "max_tokens": 3000},
    # Deep reasoning tasks — route to the reasoning tier (Sonnet 4.6 by
    # default; Opus only by explicit operator override).
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

# Provider-aware default feature models. Consulted by ClaudeClient._resolve_feature
# BEFORE the legacy DEFAULT_FEATURE_MODELS table when the provider matches.
# Operator's feature_models config still wins over both.
#
# Why per-provider rather than a single map: Anthropic operators don't want
# openrouter/-prefixed strings injected, and OpenRouter operators don't want
# bare 'claude-sonnet-4-5' strings (LiteLLM would route those Anthropic-direct,
# silently bypassing OpenRouter and breaking cost/billing/rate-limits).
DEFAULT_FEATURE_MODELS_BY_PROVIDER: dict[str, dict[str, dict[str, int | str]]] = {
    "openrouter": {
        # CI log analysis: DeepSeek V4 Flash is fast + cheap and handles
        # the bounded "what failed?" pattern well. R1 was overkill — its
        # extended-thinking surface doesn't change the verdict.
        "ci_log_analysis": {
            "model": "openrouter/deepseek/deepseek-v4-flash",
            "max_tokens": 2000,
        },
        # Complexity classifier on OpenRouter: Gemini 2.5 Flash-Lite is
        # the cheapest credible option in the opencode model registry
        # (~$0.0001/call) and easily handles a 4-way structured-output
        # classification. (Gemini 3 Flash-Lite is not in the registry
        # yet — only ``gemini-3-flash-preview`` and ``gemini-3-pro-preview``.)
        "complexity_classifier": {
            "model": "openrouter/google/gemini-2.5-flash-lite",
            "max_tokens": 300,
        },
        # Architecture review: Sonnet 4.6 (was Opus 4.6). Opus is ~5x
        # the cost for marginal quality on these tasks — operators who
        # need Opus can override via ``feature_models``.
        "principal_architecture_review": {
            "model": "openrouter/anthropic/claude-sonnet-4.6",
            "max_tokens": 4000,
        },
        # upgrade_impact_analysis has no DEFAULT_FEATURE_MODELS entry; Anthropic
        # operators resolve it to LLMConfig.default_model. The :online suffix is
        # OpenRouter-specific (web-grounded search before completion).
        "upgrade_impact_analysis": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 3000,
        },
        "migration_analysis": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 4000,
        },
        "migration_plan": {
            "model": "openrouter/anthropic/claude-sonnet-4.6:online",
            "max_tokens": 5000,
        },
        # Other features fall through to DEFAULT_FEATURE_MODELS (legacy) or
        # default_model — see ClaudeClient._resolve_feature for precedence.
    },
}


# Per-model pricing in USD per 1 MILLION tokens (input, output).
#
# Source: https://openrouter.ai/models — captured 2026-05.  Update by
# replacing the entry inline and re-running ``pytest
# tests/test_observability_cost_tracking.py``; no other code changes
# needed.  Models not present in this table emit a one-shot warning
# from :func:`caretaker.observability.metrics.record_llm_cost` and skip
# USD cost tracking — token counts are still recorded, so the dashboard
# can show "tokens by model" even for newly-added models we haven't
# priced yet.
#
# Update cadence: review each quarter, or whenever a provider changes
# their pricing publicly. The dashboard panel using
# ``caretaker_llm_cost_usd_total`` is approximate — any drift between
# this table and the provider's invoice manifests as a discrepancy
# Grafana labels "approximate (static price table)".
#
# Convention: prices are ``(input_per_1m_tokens, output_per_1m_tokens)``.
# Always quote both even if the provider charges the same for input and
# output, so a future split doesn't silently break callers.
#
# **Cache token caveat (Anthropic):** Anthropic charges 1.25× the input
# rate for ``cache_write`` tokens and 0.10× for ``cache_read`` tokens,
# but this table doesn't model that. Both are folded into
# ``prompt_tokens`` and counted at the input rate. For a cache-heavy
# review on Anthropic models, the recorded USD cost can drift ±10-15%
# from the true bill. If accuracy matters more than simplicity, extend
# the entry to a ``dataclass(input, output, cache_read=None,
# cache_write=None)`` and update
# :func:`caretaker.observability.metrics.record_llm_cost` to use the
# optional fields when present.
LLM_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Google
    "openrouter/google/gemini-2.5-flash-lite": (0.10, 0.40),
    "openrouter/google/gemini-2.5-flash": (0.30, 2.50),
    "openrouter/google/gemini-2.5-pro": (1.25, 10.00),
    "openrouter/google/gemini-3-flash-preview": (0.30, 2.50),
    # DeepSeek
    "openrouter/deepseek/deepseek-v4-flash": (0.14, 0.28),
    "openrouter/deepseek/deepseek-v4-pro": (0.55, 2.19),
    "openrouter/deepseek/deepseek-r1": (0.55, 2.19),
    # Anthropic via OpenRouter
    "openrouter/anthropic/claude-haiku-4.5": (1.00, 5.00),
    "openrouter/anthropic/claude-sonnet-4.5": (3.00, 15.00),
    "openrouter/anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "openrouter/anthropic/claude-opus-4.6": (15.00, 75.00),
    "openrouter/anthropic/claude-opus-4.7": (15.00, 75.00),
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


class ModelPoolConfig(StrictBaseModel):
    """Capability-tag → concrete-model registry consumed by the consensus engine.

    Tags are operator-defined; common values: ``fast``, ``reasoning_anthropic``,
    ``reasoning_alt``, ``cheap``. Per-site
    :class:`ConsensusDomainConfig.primary` / ``escalation`` accept either a
    tag or a literal model string accepted by the LLM router.

    The pool stays empty by default — sites that don't opt into consensus
    never look at it.
    """

    pool: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapping of capability tag → concrete model string. Every value "
            "must be a non-empty string accepted by the LLM router (e.g. "
            "'claude-sonnet-4-6', 'openai/gpt-4o', 'azure_ai/gpt-4o')."
        ),
    )

    @field_validator("pool")
    @classmethod
    def _no_empty_values(cls, value: dict[str, str]) -> dict[str, str]:
        for tag, model in value.items():
            if not isinstance(model, str) or not model:
                raise ValueError(
                    f"pool tag {tag!r} maps to invalid value {model!r}; "
                    "every tag must resolve to a non-empty model string"
                )
        return value


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
    # "openrouter" is an alias that resolves to LiteLLM under the hood but
    # enforces openrouter/-prefixed model strings (see model_validator below).
    provider: Literal["anthropic", "litellm", "openrouter"] = "anthropic"
    # Model used when a feature has no explicit override. For litellm this
    # can be prefixed (e.g. "openai/gpt-4o", "azure_ai/gpt-4o", "vertex_ai/gemini-2.5-pro").
    default_model: str = DEFAULT_MODEL
    # Per-request timeout in seconds.
    timeout_seconds: float = 60.0
    # Per-feature model/max_tokens overrides — deep-merged on top of DEFAULT_FEATURE_MODELS.
    feature_models: dict[str, FeatureModelConfig] = Field(default_factory=dict)
    # Fallback model chain — only used when provider="litellm".  Each entry is
    # a LiteLLM-format model string tried in order if the primary call fails.
    fallback_models: list[str] = Field(default_factory=list)
    # Capability-tag → model registry consumed by the consensus engine.
    # Empty by default; populated when a site opts into consensus.
    model_pool: ModelPoolConfig = Field(default_factory=ModelPoolConfig)
    # Number of retries for ``ClaudeClient.structured_complete`` when the model
    # returns malformed JSON or a payload that fails pydantic validation.
    # Set to 0 to disable the self-correcting retry loop.
    structured_output_retries: int = 1
    # Tunables for :mod:`caretaker.identity`'s optional LLM fallback when
    # classifying an unfamiliar login. Temporarily nested under ``LLMConfig``
    # until a dedicated ``AgenticConfig`` lands (T-D1); may be promoted
    # without a breaking change because callers read through this model.
    bot_identity: AgenticBotIdentityConfig = Field(default_factory=AgenticBotIdentityConfig)

    @model_validator(mode="after")
    def _validate_openrouter_prefix(self) -> LLMConfig:
        """When provider='openrouter', every model string must use the openrouter/ prefix.

        Prevents the silent bypass to Anthropic-direct that LiteLLM performs
        when given a bare 'claude-*' string — that bypass breaks billing,
        rate limits, and observability against the operator's intent.
        """
        if self.provider != "openrouter":
            return self

        bad: list[tuple[str, str]] = []
        if not self.default_model.startswith("openrouter/"):
            bad.append(("llm.default_model", self.default_model))
        for feature, override in self.feature_models.items():
            if override.model and not override.model.startswith("openrouter/"):
                bad.append((f"llm.feature_models.{feature}.model", override.model))
        for i, m in enumerate(self.fallback_models):
            if m and not m.startswith("openrouter/"):
                bad.append((f"llm.fallback_models[{i}]", m))

        if bad:
            offenders = "\n  ".join(f"{path} = {value!r}" for path, value in bad)
            raise ValueError(
                f"provider='openrouter' requires every model string to start with "
                f"'openrouter/'. Offending fields:\n  {offenders}\n"
                f"Fix each one (e.g. 'openrouter/anthropic/claude-sonnet-4.6') or "
                f"change provider to 'litellm' if you intentionally want to mix "
                f"non-openrouter prefixes."
            )
        return self


class OrchestratorConfig(StrictBaseModel):
    schedule: Literal["hourly", "daily", "weekly", "manual"] = "daily"
    summary_issue: bool = True
    dry_run: bool = False


class FleetGateConfig(StrictBaseModel):
    """Webhook delivery filter — restrict the dispatcher to an explicit
    allow-list of repos so forks / inactive installations stop generating
    fleet heartbeats and observability noise.

    When ``allowed_repos`` is empty, ALL incoming webhooks pass — this
    preserves backward compatibility with existing deployments.

    Patterns supported:
      - Exact slug: ``"owner/repo"``
      - Owner wildcard: ``"owner/*"`` (all repos under an owner)
      - Plain ``"*"`` = match anything (equivalent to leaving the list
        empty; provided so an operator can be explicit).

    The check runs at the head of ``WebhookDispatcher.dispatch`` —
    short-circuits with ``outcome="not_in_allowlist"`` before any agent
    resolution, fleet heartbeat write, or context factory call. The
    filtered repo's webhook still gets a 200 OK back to GitHub (we
    accepted delivery, we just chose not to act).
    """

    allowed_repos: list[str] = Field(default_factory=list)
    # Verbose logging — emit one INFO log per filtered delivery instead
    # of just a metric. Useful when first turning on the allow-list.
    log_filtered: bool = False


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


class ClaudeCodeLocalBackendConfig(StrictBaseModel):
    """Configuration for the ``claude_code_local`` complex-reviewer backend.

    Caretaker invokes the ``claude`` (Claude Code) CLI as a subprocess
    in its own pod, against a freshly-cloned working copy of the PR's
    head. This is an alternative to the ``claude_code`` backend (which
    triggers ``anthropics/claude-code-action`` in the target repo via a
    mention comment) — ``claude_code_local`` keeps execution centralised
    in caretaker, producing live streamed logs and removing the need to
    install a per-repo workflow.

    Designed for k8s-style deployments where caretaker runs as a
    long-lived pod with credentials in-process. For high-PR-rate or
    multi-tenant deployments, prefer the (future) k8s Job invocation
    mode so each review runs in its own pod with bounded resources.
    """

    cli_path: str = "claude"
    # Subset of tools claude is allowed to call. Defaults are read-only
    # (Read/Glob/Grep) plus Bash for ``git diff``/``git log``. Add
    # ``Edit``/``Write`` only if you intend to allow the reviewer to
    # propose patches — not the typical PR-review flow.
    allowed_tools: list[str] = Field(default_factory=lambda: ["Read", "Glob", "Grep", "Bash"])
    # ``plan`` keeps everything read-only (safest for review).
    # ``acceptEdits`` lets claude write files in the workdir, which we
    # throw away anyway, so it's safe but uses more tokens.
    permission_mode: Literal["plan", "acceptEdits", "bypassPermissions"] = "plan"
    # Hard cap on subprocess wall time. Claude Code can run many tool
    # turns; bound at 10 minutes by default so a runaway session doesn't
    # pin the pod.
    timeout_seconds: int = 600
    # Where the temp clone lives. Empty string defers to the OS temp
    # dir; pin to a fast-disk volume in production.
    clone_workdir_root: str = ""
    # Clone depth; shallow is faster but loses history-aware analyses
    # (git blame, log). 50 is a reasonable balance for most reviews.
    clone_depth: int = 50
    # Extra env passed to the subprocess. ``ANTHROPIC_API_KEY`` and
    # ``GITHUB_TOKEN`` are typical entries; caretaker will inherit any
    # already-set values from its own env unless overridden here.
    extra_env: dict[str, str] = Field(default_factory=dict)
    # When True, leave the temp clone on disk after a failed run for
    # post-mortem inspection (useful in dev; off in prod to save disk).
    keep_workdir_on_failure: bool = False


class OpenCodeLocalBackendConfig(StrictBaseModel):
    """Configuration for the ``opencode_local`` complex-reviewer backend.

    Caretaker invokes the ``opencode`` CLI as a subprocess in its own pod,
    against a freshly-cloned working copy of the PR's head. This is an
    alternative to the ``opencode`` comment-trigger backend (which posts
    ``@opencode-agent`` and waits for ``sst/opencode/github`` to reply) —
    ``opencode_local`` keeps execution centralised in caretaker, producing
    live streamed logs with no cross-cycle wait and no per-repo workflow
    install.

    The backend is automatically selected for caretaker-owned PRs when
    ``pr_reviewer.caretaker_owned_reviewer = "opencode_local"`` (default).
    """

    cli_path: str = "opencode"
    # Default review/fix models when no tier is supplied. These are also
    # the *fallback* values when ``review_models``/``fix_models`` for the
    # selected tier are empty (e.g. mis-typed tier name).
    #
    # Model selection philosophy: prefer faster balanced models (Gemini
    # 2.5 Pro / DeepSeek V4) over the most expensive Anthropic models
    # (Opus). Coding agents lean on Gemini 2.5 Pro and Claude Sonnet
    # 4.5 which are faster and cheaper for the same code-review /
    # code-edit quality.
    #
    # All defaults route through OpenRouter since that's the provider
    # registered in opencode CLI's model registry today. Operators with
    # direct GEMINI_API_KEY / OPENAI_API_KEY in the pod env can swap
    # to ``google/...`` or ``openai/...`` strings via config override.
    #
    # IMPORTANT: model IDs here have been verified against opencode's
    # registry AND OpenRouter's actual provider availability. Earlier
    # v0.28.x defaults used ``gemini-3-pro-preview`` which exists in
    # opencode's registry but returns ``No endpoints found`` from
    # OpenRouter. Use ``opencode run "ping" --model <id>`` to verify
    # any new model before adding it.
    model: str = "openrouter/google/gemini-2.5-pro"
    fix_model: str = "openrouter/google/gemini-2.5-pro"
    # Tier → model map for PR *review*. Keys are the four tiers from
    # :mod:`caretaker.pr_reviewer.complexity_classifier`. Gemini-first:
    # Flash-Lite for trivial typo work, Flash for simple bug fixes,
    # DeepSeek V4 Flash for ordinary feature work, Gemini 2.5 Pro for
    # genuinely complex review (large refactors, sensitive paths).
    review_models: dict[str, str] = Field(
        default_factory=lambda: {
            "trivial": "openrouter/google/gemini-2.5-flash-lite",
            "simple": "openrouter/google/gemini-2.5-flash",
            "standard": "openrouter/deepseek/deepseek-v4-flash",
            "complex": "openrouter/google/gemini-2.5-pro",
        }
    )
    # Tier → model map for the auto-fix pass. Fix needs to actually
    # write code, so the floor is higher than for review — Flash-Lite
    # struggles with multi-file edits. Gemini 2.5 Flash for trivial
    # mechanical fixes, Haiku 4.5 for simple isolated fixes, DeepSeek
    # V4 Pro for standard work, Sonnet 4.5 for complex multi-file edits
    # (more reliable than Gemini Pro for code-edit quality).
    fix_models: dict[str, str] = Field(
        default_factory=lambda: {
            "trivial": "openrouter/google/gemini-2.5-flash",
            "simple": "openrouter/anthropic/claude-haiku-4.5",
            "standard": "openrouter/deepseek/deepseek-v4-pro",
            "complex": "openrouter/anthropic/claude-sonnet-4.5",
        }
    )
    timeout_seconds: int = 600
    # Directory where PR clones are created.  Empty string defers to the
    # OS temp dir.  In k8s deployments, pin to the emptyDir volume mount
    # (e.g. ``/tmp/caretaker-pr-review``) so clones land on the fast
    # ephemeral volume rather than the container's overlay filesystem.
    clone_workdir_root: str = ""
    clone_depth: int = 50
    # Extra env passed to the subprocess. ``OPENROUTER_API_KEY`` and
    # ``GITHUB_TOKEN`` are already in the pod env and do not need to be
    # repeated here unless you want to override them for this backend.
    extra_env: dict[str, str] = Field(default_factory=dict)
    keep_workdir_on_failure: bool = False


class AutoFixConfig(StrictBaseModel):
    """Configuration for the PR-reviewer auto-fix loop.

    When the reviewer returns ``REQUEST_CHANGES``, caretaker can dispatch
    a *fixer* backend (a coding agent or a deterministic linter) to
    address the feedback, push the result, and re-review. Disabled by
    default — opt-in per PR via the ``opt_in_label`` (``caretaker:auto-fix``
    by default), or opt-in per author via ``allowed_authors`` for
    bot-only fleets.

    Routing: each ``IssueCategory`` (lint, security, …) maps to a
    backend name in ``category_to_fixer``. When the reviewer's verdict
    omits categories, a keyword heuristic runs over the summary text;
    when that also fails, ``default_fixer`` is used. Pairing a different
    fixer than reviewer (e.g. reviewer=claude_code_local,
    fixer=claude_code_local for security but ``deterministic_lint`` for
    lint) avoids the trust-spiral where reviewer and fixer mutually
    validate each other's mistakes.
    """

    enabled: bool = True
    # Hard cap on dispatched fixer attempts per PR. After this many
    # attempts without a green review, caretaker stops and escalates
    # rather than burning unbounded budget. Reset on a force-push that
    # changes the PR head SHA (so a human edit re-arms the loop).
    max_attempts: int = 3
    # Label that opts a single PR into the loop. Match exactly.
    opt_in_label: str = "caretaker:auto-fix"
    # PR authors that are auto-eligible *without* the opt-in label —
    # typically bot accounts whose PRs are caretaker's responsibility
    # anyway. Human-authored PRs always require the label.
    allowed_authors: list[str] = Field(
        default_factory=lambda: [
            "ianlintner",
            "Copilot",
            "copilot-swe-agent[bot]",
            "github-actions[bot]",
            "dependabot[bot]",
            "the-care-taker[bot]",
        ]
    )
    # Map each issue category to the backend that handles it. Special
    # value ``deterministic_lint`` runs ``ruff format && ruff check
    # --fix`` (or the configured commands) instead of an LLM — cheap,
    # zero-cost, no trust risk. Unknown categories or unmapped values
    # fall back to ``default_fixer``.
    category_to_fixer: dict[str, str] = Field(
        default_factory=lambda: {
            "lint": "deterministic_lint",
            "format": "deterministic_lint",
            "type": "claude_code_local",
            "test": "claude_code_local",
            "security": "claude_code_local",
            "correctness": "claude_code_local",
            "docs": "claude_code_local",
            "other": "claude_code_local",
        }
    )
    default_fixer: str = "claude_code_local"
    # Commands run by the ``deterministic_lint`` fixer in order. Each is
    # ``shell=False`` so list-of-args. Keep the set small and idempotent
    # so repeated runs converge.
    deterministic_lint_commands: list[list[str]] = Field(
        default_factory=lambda: [
            ["ruff", "format", "."],
            ["ruff", "check", "--fix", "."],
        ]
    )
    # Commit message used when caretaker pushes a fix. Append the
    # category as a suffix when known.
    fix_commit_message: str = "fix: address review feedback (caretaker auto-fix)"
    # When True, caretaker checks the heuristic classifier even when the
    # reviewer supplied ``issue_categories`` and merges the union.
    # Default False — trust the LLM's classification when present.
    always_run_heuristic: bool = False


class PRAgentBackendConfig(StrictBaseModel):
    """Configuration for the ``pr_agent`` complex-reviewer backend.

    Caretaker invokes the open-source PR-Agent CLI
    (https://github.com/The-PR-Agent/pr-agent) as a subprocess to produce
    a review for high-complexity PRs. PR-Agent is AGPL-3.0; running it as
    a separate process keeps the licence boundary at "aggregation" — no
    Python imports, no in-process linking. Tighten ``cli_path`` to a
    pinned path in production deployments where you control the binary.
    """

    cli_path: str = "pr-agent"
    command: str = "review"
    timeout_seconds: int = 180
    # Extra environment variables passed through to the pr-agent
    # subprocess (e.g. ``OPENAI_KEY``, ``ANTHROPIC_KEY``, ``GITHUB_TOKEN``,
    # ``CONFIG.MODEL``). Caretaker does NOT inject defaults here so
    # operators stay in explicit control of which credentials reach the
    # third-party process.
    extra_env: dict[str, str] = Field(default_factory=dict)
    # When True, also post pr-agent's raw markdown output as an issue
    # comment (in addition to the formal Reviews-tab entry) so reviewers
    # can see pr-agent's full reasoning. Off by default to keep the PR
    # thread tidy.
    post_raw_output_comment: bool = False


class PRReviewerConfig(StrictBaseModel):
    """Configuration for the dual-path PR code reviewer.

    When ``enabled`` is ``True``, caretaker reviews opened/updated PRs:
    - Low-complexity PRs (score < ``routing_threshold``) get an inline
      LLM review posted as a GitHub pull-request review.
    - High-complexity PRs get handed off to a configurable backend
      (``claude_code``, ``opencode``, ``pr_agent``, …). ``claude_code``
      and ``opencode`` use the comment-trigger pattern (label + mention,
      reply harvested next cycle); ``pr_agent`` runs the third-party CLI
      in-process and posts the formal review directly.

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
    # Score threshold: score >= threshold → complex hand-off; else inline LLM.
    routing_threshold: int = 40
    # Which BYOCA coding agent to use for complex PR reviews. Must match
    # a registered agent name (``claude_code``, ``opencode``, …) or the
    # special value ``inline`` to keep everything inline.
    complex_reviewer: str = "opencode_local"
    # Label/mention used for the claude-code-action hand-off.
    # Retained for backward-compatibility — read by the claude_code
    # reviewer dispatch path. New deployments should configure the
    # equivalent on the registered agent instead.
    claude_code_label: str = "claude-code"
    claude_code_mention: str = "@claude"
    # Label/mention used for the opencode hand-off when
    # ``complex_reviewer = "opencode"``.
    opencode_label: str = "opencode-review"
    opencode_mention: str = "@opencode-agent"
    # Label/mention used for the CodeRabbit comment-trigger stub. The
    # CodeRabbit GitHub App reads ``@coderabbitai`` mentions; caretaker
    # only posts the trigger and harvests the response (parser stub).
    coderabbit_label: str = "coderabbit-review"
    coderabbit_mention: str = "@coderabbitai review"
    # Settings for the pr-agent CLI backend (``complex_reviewer = "pr_agent"``).
    pr_agent: PRAgentBackendConfig = Field(default_factory=PRAgentBackendConfig)
    # Settings for the local Claude Code CLI backend
    # (``complex_reviewer = "claude_code_local"``). Distinct from
    # ``claude_code`` (comment-trigger via GitHub Action).
    claude_code_local: ClaudeCodeLocalBackendConfig = Field(
        default_factory=ClaudeCodeLocalBackendConfig
    )
    # Settings for the local opencode CLI backend.  Used when
    # ``caretaker_owned_reviewer = "opencode_local"`` (the default for
    # PRs authored by ``the-care-taker[bot]``).
    opencode_local: OpenCodeLocalBackendConfig = Field(default_factory=OpenCodeLocalBackendConfig)
    # Backend used to review PRs authored by caretaker itself.  Defaults
    # to ``"opencode_local"`` (in-pod subprocess, synchronous, no
    # comment-trigger round-trip).  Set to ``""`` to use the standard
    # ``complex_reviewer`` path for caretaker-owned PRs instead.
    caretaker_owned_reviewer: str = "opencode_local"
    # Auto-fix loop: when reviewer returns REQUEST_CHANGES, dispatch a
    # fixer backend (coding agent or deterministic linter), push the
    # result, re-review. Disabled by default; opt-in per-PR via
    # ``caretaker:auto-fix`` label or per-author via allowed_authors.
    auto_fix: AutoFixConfig = Field(default_factory=AutoFixConfig)
    # Backends caretaker is allowed to dispatch to. Only specs in this
    # list can be selected via ``complex_reviewer``; misconfiguration
    # surfaces at startup rather than at PR-review time. Stub backends
    # (``coderabbit``, ``greptile``) are registered in the spec registry
    # but absent here by default — opt them in explicitly.
    # ``claude_code_local`` is also opt-in: it requires the ``claude``
    # CLI installed in caretaker's pod and an ANTHROPIC_API_KEY.
    enabled_backends: list[str] = Field(
        default_factory=lambda: ["claude_code", "opencode", "pr_agent"]
    )
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
    # When True, call the approve endpoint. When False the agent only
    # *surfaces* stuck runs in the digest and as a maintainer:escalated
    # issue hint — no side effects on GitHub.
    # Defaulting to True because the ``allowed_actors`` list is intentionally
    # tight (bots only) and the common fleet issue is upgrade PRs that stall
    # forever awaiting a manual Actions UI click.
    auto_approve: bool = True
    # Maximum runs to process per caretaker run (cap API usage).
    max_runs_per_run: int = 25
    # Skip runs older than this many hours (avoids approving ancient runs
    # that have been superseded by later pushes).
    max_age_hours: int = 48
    # Only act on runs whose event is in this set. ``pull_request`` is the
    # common case; ``issue_comment`` covers @copilot nudges that re-trigger
    # workflows.
    trigger_events: list[str] = Field(default_factory=lambda: ["pull_request", "issue_comment"])


class MemoryStoreConfig(StrictBaseModel):
    """Configuration for the disk-backed agent memory store."""

    enabled: bool = True
    # Storage backend: "sqlite" (default, zero-dependency), "mongo" (Phase 1, requires
    # MongoConfig.enabled=true and MONGODB_URL env var set), "redis" (requires
    # RedisConfig.enabled=true and REDIS_URL env var set), or "neo4j" (requires
    # GraphStoreConfig.enabled=true and NEO4J_URL / NEO4J_AUTH env vars set).
    backend: Literal["sqlite", "mongo", "redis", "neo4j"] = "sqlite"
    # Path to the SQLite database file.  A relative path is resolved from the
    # current working directory (i.e. the GitHub Actions workspace root).
    # Ignored when backend is not "sqlite".
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

    Example ``.github/maintainer/config.yml``::

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

    Example ``.github/maintainer/config.yml``::

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


class CodingJobsConfig(StrictBaseModel):
    """Durable K8s coding job dispatch via Azure Service Bus + Redis Streams."""

    enabled: bool = False
    # ASB — queue layer
    asb_namespace: str = ""  # e.g. thebiggestboy.servicebus.windows.net
    asb_queue_coding_tasks: str = "coding-tasks"
    asb_lock_duration_secs: int = 300  # must match queue LockDuration in portal
    # Redis — job-status stream only
    stream_job_status: str = "job-status"
    status_consumer_group: str = "coding-results"
    # K8s
    k8s_namespace: str = "caretaker"
    k8s_worker_image: str = ""
    per_attempt_timeout_secs: int = 900  # matches activeDeadlineSeconds
    # Reconciler
    heartbeat_staleness_secs: int = 300
    reconcile_interval_secs: int = 30


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

    This is the **canonical** authentication mode for caretaker
    service-to-service calls (fleet heartbeat, MCP backend access, and
    any future authenticated endpoints). When ``enabled`` is ``True``
    AND all three env vars named by ``client_id_env`` /
    ``client_secret_env`` / ``token_url_env`` are populated, consumers
    like :class:`FleetRegistryConfig` will attach a bearer token to
    outbound requests.

    The default names match the conventional ``OAUTH2_CLIENT_ID`` /
    ``OAUTH2_CLIENT_SECRET`` / ``OAUTH2_TOKEN_URL`` triple that caretaker
    writes into consumer repos when an operator provisions client
    credentials against a shared authorization server. ``default_scope``
    defaults to ``fleet:heartbeat`` because the fleet heartbeat is the
    only currently-authenticated endpoint; per-resource configs may
    override.
    """

    enabled: bool = False
    client_id_env: str = "OAUTH2_CLIENT_ID"
    client_secret_env: str = "OAUTH2_CLIENT_SECRET"
    token_url_env: str = "OAUTH2_TOKEN_URL"
    scope_env: str = "OAUTH2_SCOPE"
    # Requested scope if ``scope_env`` is not populated. ``fleet:heartbeat``
    # is the default because that is the only authenticated public endpoint
    # caretaker currently exposes; override per-config when other resources
    # need different scopes.
    default_scope: str = "fleet:heartbeat"
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

    Authentication is **OAuth2 client_credentials only**. When the
    nested ``oauth2`` block is enabled and its env vars are populated,
    the emitter fetches a bearer token via the OAuth2
    ``client_credentials`` grant and sends it in the ``Authorization``
    header. The backend rejects unauthenticated heartbeats.

    ``secret_env`` is **deprecated** and retained only for backwards
    compatibility with older configs; it is no longer consulted by the
    emitter or backend.
    """

    enabled: bool = False
    endpoint: str | None = None
    # Deprecated: HMAC heartbeat signing has been removed in favour of
    # OAuth2 client_credentials. This field is retained so older
    # config-default.yml files still validate; its value is ignored.
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


class HandoffAgentConfig(StrictBaseModel):
    """Configuration shared by every BYOCA hand-off coding agent.

    Hand-off agents (Claude Code, opencode, …) all behave identically:
    apply a trigger label to the host PR / issue, post a structured
    ``@mention`` comment, and let an upstream GitHub Action installed on
    the consumer repo produce the fix asynchronously. Caretaker tracks
    the resulting commit / PR through the same ``<!-- caretaker:result -->``
    markers it already uses for the Copilot + Foundry paths.

    Each concrete agent supplies its own defaults for ``trigger_label`` /
    ``mention``; operators can override them per repo via the
    ``executor.agents.<name>`` config block.
    """

    enabled: bool = False
    # Execution mode. Phase 1 only ships ``handoff``; ``inline`` and
    # ``k8s_job`` are reserved for future phases. Validated at startup
    # against the agent class — opencode/claude_code are hand-off only.
    mode: Literal["handoff", "inline", "k8s_job"] = "handoff"
    # Label caretaker applies to trigger the upstream workflow. Empty
    # string falls through to the agent class's ``default_trigger_label``.
    trigger_label: str = ""
    # Mention string included in the hand-off comment so the upstream
    # auto-detector can pick it up even if a repo has a different label
    # listener name configured. Empty string falls through to the agent
    # class's ``default_mention``.
    mention: str = ""
    # Maximum attempts per task before caretaker stops re-applying the
    # trigger label; prevents ping-pong if the upstream action can't
    # complete the work.
    max_attempts: int = 2


class ClaudeCodeExecutorConfig(HandoffAgentConfig):
    """Configuration for the ``anthropics/claude-code-action`` hand-off agent.

    Identical shape to :class:`HandoffAgentConfig`; subclassed only so
    legacy ``executor.claude_code: …`` YAML keeps working without an
    ``extra="forbid"`` validation error during the deprecation window.
    The defaults match what shipped before the BYOCA refactor.

    See https://github.com/anthropics/claude-code-action for the upstream
    action this agent dispatches to.
    """

    trigger_label: str = "claude-code"
    mention: str = "@claude"


class OpenCodeExecutorConfig(HandoffAgentConfig):
    """Configuration for the ``sst/opencode``-style hand-off agent.

    opencode (https://github.com/sst/opencode) supports many providers in
    agent mode — useful when caretaker is dispatching coding work in repos
    that need a non-Anthropic backend. Caretaker treats it as a peer of
    Claude Code: same hand-off shape, different label / mention so each
    upstream workflow can listen on its own trigger and per-PR attempt
    counts don't cross-contaminate.

    Feature is opt-in (``enabled = False`` by default); the consumer repo
    must have the upstream opencode action installed and authorised on
    its own. The maintainer agent's template installer can write the
    ``.github/workflows/opencode.yml`` workflow into consumer repos when
    they opt in; see ``setup-templates/templates/.github/workflows/``.
    """

    trigger_label: str = "opencode"
    mention: str = "@opencode-agent"


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
    """Top-level switch deciding how coding tasks are executed.

    BYOCA — Bring Your Own Coding Agent. ``provider`` is a string naming
    a registered :class:`~caretaker.coding_agents.protocol.CodingAgent`
    plus the legacy reserved values ``copilot`` (always available; never
    in the registry) and ``auto`` (try the registered custom agents in
    order, fall back to Copilot).

    Built-in registered names today: ``foundry``, ``claude_code``,
    ``opencode``. Operators can register additional agents (codex,
    gemini, hermes, …) by populating the ``agents`` map below. Unknown
    ``provider`` values are diagnosed at orchestrator startup with the
    full list of registered names — not at config-parse time, so
    operators get a useful error.
    """

    provider: str = "copilot"
    foundry: FoundryExecutorConfig = Field(default_factory=FoundryExecutorConfig)
    claude_code: ClaudeCodeExecutorConfig = Field(default_factory=ClaudeCodeExecutorConfig)
    opencode: OpenCodeExecutorConfig = Field(default_factory=OpenCodeExecutorConfig)
    k8s_worker: K8sAgentWorkerConfig = Field(default_factory=K8sAgentWorkerConfig)
    # Per-repo overrides for additional registered agents. Keys are agent
    # names (matching the agent's ``CodingAgent.name``); values follow
    # :class:`HandoffAgentConfig`. Use this for agents that aren't built
    # in (codex, gemini, hermes, …) without breaking the existing typed
    # ``claude_code`` / ``opencode`` blocks.
    agents: dict[str, HandoffAgentConfig] = Field(default_factory=dict)


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


class ConsensusDomainConfig(StrictBaseModel):
    """Per-decision-site consensus engine configuration.

    Attached as an optional field to :class:`AgenticDomainConfig`. When
    ``None``, the existing single-model path runs (no engine involved).
    Sites opt in by setting this in YAML.

    Tag values resolve through :class:`LLMConfig.model_pool`. Literal model
    strings (anything not a known tag) pass through unchanged to the LLM
    router.
    """

    strategy: Literal["tiered_confidence", "always_two_models"] = "tiered_confidence"
    primary: str = Field(
        default="fast",
        description="Capability tag or literal model string for the primary call.",
    )
    escalation: list[str] = Field(
        default_factory=lambda: ["reasoning_anthropic"],
        description=(
            "Ordered tags/literals for escalation. TieredConfidence consults "
            "every entry on low-confidence; AlwaysTwoModels uses [0] as the "
            "second voter and [1:] as tiebreakers."
        ),
    )
    confidence_threshold: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="TieredConfidence escalates when verdict.confidence < this.",
    )
    agreement_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Field names compared for AlwaysTwoModels agreement. Empty list "
            "means compare full verdicts via ==. For readiness, set to "
            "['verdict'] so only the closed-enum field has to match."
        ),
    )

    @model_validator(mode="after")
    def _validate_strategy_specific(self) -> ConsensusDomainConfig:
        if self.strategy == "always_two_models":
            if not self.escalation:
                raise ValueError(
                    "always_two_models requires escalation[0] (no second model configured)"
                )
            if self.escalation[0] == self.primary:
                raise ValueError(
                    f"always_two_models requires escalation[0] ({self.escalation[0]!r}) "
                    f"to be distinct from primary ({self.primary!r}); use a different "
                    "tag or literal model so the two-model gate consults distinct models"
                )
        return self


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
    # Optional consensus engine config. When set, the site routes its LLM
    # path through the engine; when None, the existing single-model path
    # (claude.structured_complete) runs unchanged.
    consensus: ConsensusDomainConfig | None = None


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
    executor_routing: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    crystallizer_category: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)
    # Foundry's pre/post-flight sizing gate. Today the gate is a pure
    # heuristic (file count + line count). With a non-None ``consensus``
    # field, borderline cases consult the engine to judge whether a diff
    # in the gray zone is mechanical (route to Foundry) or genuinely
    # complex (escalate to Copilot).
    size_classifier: AgenticDomainConfig = Field(default_factory=AgenticDomainConfig)


class MaintainerConfig(StrictBaseModel):
    version: Literal["v1"] = "v1"
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    # Optional foundry/copilot executor routing. Omit (or leave at default
    # provider=copilot) to preserve legacy behavior byte-identically.
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    pr_agent: PRAgentConfig = Field(default_factory=PRAgentConfig)
    issue_agent: IssueAgentConfig = Field(default_factory=IssueAgentConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    # Shepherd mode — PR cleanup loop codified from the manual Apr-24 session
    # (see memory/project_pr_shepherd.md when added). Disabled by default; the
    # default-constructed config keeps current behavior byte-identical.
    shepherd: ShepherdConfig = Field(default_factory=ShepherdConfig)
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
    # Webhook delivery filter — when ``allowed_repos`` is set, the
    # dispatcher short-circuits webhooks for repos outside the list so
    # inactive forks stop generating heartbeat / observability noise.
    # Empty list (default) preserves current behavior (allow all).
    fleet_gate: FleetGateConfig = Field(default_factory=FleetGateConfig)
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
