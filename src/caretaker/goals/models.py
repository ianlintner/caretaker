"""Data models for the goal-seeking subsystem."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GoalStatus(StrEnum):
    """Health status of a goal based on its score trajectory."""

    SATISFIED = "satisfied"
    PROGRESSING = "progressing"
    STALE = "stale"
    DIVERGING = "diverging"
    CRITICAL = "critical"
    ESCALATED = "escalated"


class GoalSnapshot(BaseModel):
    """Point-in-time evaluation of a single goal."""

    goal_id: str
    score: float  # 0.0 (unmet) to 1.0 (fully satisfied)
    status: GoalStatus = GoalStatus.PROGRESSING
    details: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GoalEscalation(BaseModel):
    """An escalation triggered by a goal's poor or worsening health."""

    goal_id: str
    status: GoalStatus
    score: float
    reason: str
    consecutive_runs: int = 0
    recommended_action: str = ""


class GoalEvaluation(BaseModel):
    """Complete evaluation of all goals for a single orchestrator run."""

    snapshots: dict[str, GoalSnapshot] = Field(default_factory=dict)
    overall_health: float = 0.0
    dispatch_plan: list[str] = Field(default_factory=list)
    escalations: list[GoalEscalation] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
