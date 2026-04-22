"""Evolution layer — learn-and-adapt subsystem for caretaker.

Provides InsightStore (skill memory), ReflectionEngine, StrategyMutator,
PlanMode, AgentFileEvolver to close the learn-and-adapt gap, plus the
Phase-2 ``@shadow_decision`` infrastructure that lets LLM handovers
run side-by-side with existing heuristics.
"""

from caretaker.evolution.insight_store import InsightStore, Skill
from caretaker.evolution.shadow import (
    ShadowDecisionRecord,
    ShadowMode,
    ShadowOutcome,
    shadow_decision,
)

__all__ = [
    "InsightStore",
    "ShadowDecisionRecord",
    "ShadowMode",
    "ShadowOutcome",
    "Skill",
    "shadow_decision",
]
