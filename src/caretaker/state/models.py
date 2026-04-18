"""State data models for tracking orchestrator activity."""

from __future__ import annotations

from datetime import datetime
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


class TrackedIssue(BaseModel):
    number: int
    state: IssueTrackingState = IssueTrackingState.NEW
    classification: str = ""
    assigned_pr: int | None = None
    last_checked: datetime | None = None
    escalated: bool = False


class RunSummary(BaseModel):
    run_at: datetime = Field(default_factory=datetime.utcnow)
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
