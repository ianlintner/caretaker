"""State data models for tracking orchestrator activity."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from caretaker.goals.models import GoalSnapshot  # noqa: TC001 (Pydantic needs runtime access)


class OwnershipState(StrEnum):
    UNOWNED = "unowned"
    OWNED = "owned"
    RELEASED = "released"
    ESCALATED = "escalated"


class PRTrackingState(StrEnum):
    DISCOVERED = "discovered"
    CI_PENDING = "ci_pending"
    CI_PASSING = "ci_passing"
    CI_FAILING = "ci_failing"
    REVIEW_PENDING = "review_pending"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    FIX_REQUESTED = "fix_requested"
    FIX_IN_PROGRESS = "fix_in_progress"
    MERGE_READY = "merge_ready"
    MERGED = "merged"
    ESCALATED = "escalated"
    CLOSED = "closed"


class IssueTrackingState(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    PR_OPENED = "pr_opened"
    COMPLETED = "completed"
    STALE = "stale"
    ESCALATED = "escalated"
    CLOSED = "closed"


class TrackedPR(BaseModel):
    number: int
    state: PRTrackingState = PRTrackingState.DISCOVERED
    first_seen_at: datetime | None = None
    merged_at: datetime | None = None
    ci_attempts: int = 0
    copilot_attempts: int = 0
    # Timestamp of the most recent @copilot fix-request comment posted on
    # this PR. Used by `retry_window_hours`: when the prior attempt was
    # longer ago than the window, copilot_attempts resets to 0 instead of
    # tripping max_retries. This avoids escalating long-lived PRs whose
    # earlier failed attempts have aged out of relevance.
    last_copilot_attempt_at: datetime | None = None
    last_task_comment_id: int | None = None
    last_checked: datetime | None = None
    escalated: bool = False
    notes: str = ""

    # Ownership fields
    ownership_state: OwnershipState = OwnershipState.UNOWNED
    owned_by: str = "caretaker"
    ownership_acquired_at: datetime | None = None
    ownership_released_at: datetime | None = None

    # Readiness fields
    readiness_score: float = 0.0
    readiness_blockers: list[str] = Field(default_factory=list)
    readiness_summary: str = ""

    # Evolution: within-run stuck detection (Phase 5)
    fix_cycles: int = 0
    last_state_change_at: datetime | None = None
    stuck_reflection_done: bool = False

    # One-shot legacy comment compaction. Pre-#403 PRs accumulated separate
    # ownership-claim / readiness-update comments per cycle; this flag tracks
    # whether the cleanup pass has already collapsed them into the single
    # caretaker:status comment.
    legacy_comments_compacted: bool = False

    # One-shot terminal-state finalization for the ``caretaker/pr-readiness``
    # check. Once a PR is merged or closed, caretaker publishes a final
    # ``success`` / ``neutral`` conclusion so the check stops dangling
    # ``in_progress`` (PR #609 was the motivating incident — its readiness
    # check stayed in_progress for hours after merge). The flag prevents
    # republishing on every subsequent webhook for the same closed PR.
    readiness_check_finalized: bool = False

    # Names of bot reviewers (CheckRun jobs or comment authors) whose
    # approval was counted toward the "Required reviews satisfied" gate.
    # Populated from :class:`ReviewEvaluation`. Drives the ``(bot)`` label
    # rendered in the status-comment Reviews row so a human reader can
    # tell at a glance whether the green checkmark came from a bot or a
    # person.
    bot_approvers: list[str] = Field(default_factory=list)

    # ID(s) of agent reply comments whose ``caretaker-review`` JSON
    # payload caretaker has already harvested and re-posted as a formal
    # PR review via :mod:`caretaker.pr_reviewer.handoff_review_consumer`.
    # Without this set, every cycle would re-post the same review when a
    # hand-off agent (Claude Code / opencode) leaves their structured
    # reply on the PR. Comment IDs are GitHub's monotonically-issued
    # integers so duplicates are impossible across re-runs.
    consumed_handoff_review_comment_ids: list[int] = Field(default_factory=list)

    # ── Attribution telemetry (R&D workstream A2) ────────────────────────
    # Answer "did caretaker actually save human toil on this PR?" — a
    # per-PR audit trail that the weekly rollup aggregates into
    # ``caretaker_pr_outcome_total``. All three fields default to ``False``
    # / empty list; persisted Pydantic JSON round-trips them via default
    # population on load (no backend migration required — see
    # :class:`AttributionConfig` for the migration-strategy knob).
    #
    # ``caretaker_touched`` — True as soon as any caretaker agent took a
    # non-read-only action on the PR (labelled, commented, approved,
    # merged, closed). Never clears back to False once set.
    caretaker_touched: bool = False
    # ``caretaker_merged`` — True when caretaker's merge-authority path
    # actually closed the PR as merged (as opposed to the human pressing
    # "Merge"). Implies ``caretaker_touched = True``.
    caretaker_merged: bool = False
    # ``operator_intervened`` — True when a human (non-bot actor) made a
    # pushing change AFTER caretaker's most recent action on the PR:
    # a commit, a manual merge, a close, a label change, a force-push.
    # The intervention detector rewrites this every cycle by comparing
    # the persisted last-caretaker-action timestamp against the PR's
    # event timeline.
    operator_intervened: bool = False
    # ``intervention_reasons`` — short codes describing what the human
    # did. Bounded enum: ``manual_merge``, ``manual_close``,
    # ``label_changed``, ``force_push``, ``commit_added``. Appended to
    # each cycle the detector finds a new intervention; duplicates are
    # suppressed so the list grows monotonically without unbounded churn.
    intervention_reasons: list[str] = Field(default_factory=list)
    # ``last_caretaker_action_at`` — timestamp of the most recent
    # caretaker action. Used by the intervention detector as the cutoff:
    # any human activity strictly after this timestamp counts as
    # "pushed work after caretaker's last action."
    last_caretaker_action_at: datetime | None = None

    # ── Auto-approve idempotency ─────────────────────────────────────────
    # Head SHA of the most recent successful caretaker auto-approval. The
    # PR-agent's _handle_review_approve consults this before submitting an
    # APPROVE review: if it equals pr.head_sha the review is skipped, which
    # prevents duplicate approval reviews when the state-machine fires
    # ``request_review_approve`` more than once across concurrent webhook /
    # scheduled runs. Cleared (left None) on first observation, set on
    # successful approval; a new push to the PR moves head_sha forward and
    # naturally re-arms the gate.
    last_approved_sha: str | None = None


class TrackedIssue(BaseModel):
    number: int
    state: IssueTrackingState = IssueTrackingState.NEW
    classification: str = ""
    assigned_pr: int | None = None
    last_checked: datetime | None = None
    escalated: bool = False

    # ── Attribution telemetry (R&D workstream A2) ────────────────────────
    # Mirrors :class:`TrackedPR` attribution fields with issue semantics.
    # ``caretaker_closed`` replaces ``caretaker_merged`` because issues
    # don't merge — they close via the caretaker triage / stale / charlie
    # paths.
    caretaker_touched: bool = False
    caretaker_closed: bool = False
    operator_intervened: bool = False
    intervention_reasons: list[str] = Field(default_factory=list)
    last_caretaker_action_at: datetime | None = None


class RunSummary(BaseModel):
    run_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mode: str = "full"
    prs_monitored: int = 0
    prs_merged: int = 0
    prs_escalated: int = 0
    issues_triaged: int = 0
    issues_assigned: int = 0
    issues_closed: int = 0
    issues_escalated: int = 0
    orphaned_prs: int = 0
    stale_assignments_escalated: int = 0
    prs_fix_requested: int = 0
    avg_time_to_merge_hours: float = 0.0
    escalation_rate: float = 0.0
    copilot_success_rate: float = 0.0
    upgrade_available: bool = False
    upgrade_version: str = ""
    # DevOps agent metrics
    build_failures_detected: int = 0
    build_fix_issues_created: int = 0
    # CI failures the DevOps agent routed onto an open PR (commenting at
    # the reviewer / @copilot) instead of opening a parallel issue.
    build_fix_pr_comments_posted: int = 0
    # Self-heal agent metrics
    self_heal_failures_analyzed: int = 0
    self_heal_local_issues: int = 0
    self_heal_upstream_bugs: int = 0
    self_heal_upstream_features: int = 0
    # Security agent metrics
    security_findings_found: int = 0
    security_issues_created: int = 0
    security_false_positives: int = 0
    # Dependency agent metrics
    dependency_prs_reviewed: int = 0
    dependency_prs_auto_merged: int = 0
    dependency_major_issues: int = 0
    # Docs agent metrics
    docs_prs_analyzed: int = 0
    docs_pr_opened: int | None = None
    # Charlie agent metrics
    charlie_managed_issues: int = 0
    charlie_managed_prs: int = 0
    charlie_issues_closed: int = 0
    charlie_prs_closed: int = 0
    charlie_duplicates_closed: int = 0
    # Stale agent metrics
    stale_issues_warned: int = 0
    stale_issues_closed: int = 0
    stale_branches_deleted: int = 0
    # Escalation agent metrics
    escalation_items_found: int = 0
    escalation_digest_issue: int | None = None
    # Principal agent metrics
    principal_reviews: int = 0
    principal_prds_created: int = 0
    principal_refactors_planned: int = 0
    # Test agent metrics
    test_prs_analyzed: int = 0
    test_skeletons_generated: int = 0
    test_flaky_detected: int = 0
    # Refactor agent metrics
    refactor_smells_found: int = 0
    refactor_prs_created: int = 0
    # Performance agent metrics
    perf_prs_analyzed: int = 0
    perf_regressions_flagged: int = 0
    # Migration agent metrics
    migration_deprecations_found: int = 0
    migration_fixes_applied: int = 0
    # PR CI approver metrics
    ci_runs_stuck: int = 0
    ci_runs_approved: int = 0
    ci_runs_surfaced: int = 0
    # Goal engine metrics
    goal_health: float | None = None
    goal_escalation_count: int = 0
    # Review agent metrics
    reviews_completed: int = 0
    review_artifacts_written: int = 0
    review_average_score: float = 0.0
    # Ownership & readiness metrics (Phase 1)
    owned_prs: int = 0
    readiness_pass_rate: float = 0.0
    avg_readiness_score: float = 0.0
    authority_merges: int = 0
    errors: list[str] = Field(default_factory=list)


class OrchestratorState(BaseModel):
    tracked_prs: dict[int, TrackedPR] = Field(default_factory=dict)
    tracked_issues: dict[int, TrackedIssue] = Field(default_factory=dict)
    # Signatures of build failures already reported (devops agent dedup)
    reported_build_sigs: list[str] = Field(default_factory=list)
    # Signatures of self-heal issues already filed
    reported_self_heal_sigs: list[str] = Field(default_factory=list)
    # Cooldown tracking: maps coarse key (job:kind) → ISO datetime of last issue creation
    issue_cooldowns: dict[str, str] = Field(default_factory=dict)
    # Goal engine: per-goal score history for divergence detection
    goal_history: dict[str, list[GoalSnapshot]] = Field(default_factory=dict)
    last_run: RunSummary | None = None
    run_history: list[RunSummary] = Field(default_factory=list)
    # Evolution: active recovery plan milestones per goal (Phase 6)
    active_plan_ids: dict[str, int] = Field(default_factory=dict)
    # Evolution: last-activated timestamp per goal (ISO8601) — used to enforce cooldown
    plan_cooldowns: dict[str, str] = Field(default_factory=dict)
