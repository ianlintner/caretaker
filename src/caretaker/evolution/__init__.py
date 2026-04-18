"""Evolution layer — learn-and-adapt subsystem for caretaker.

Provides InsightStore (skill memory), ReflectionEngine, StrategyMutator,
PlanMode, and AgentFileEvolver to close the learn-and-adapt gap.
"""

from caretaker.evolution.insight_store import InsightStore, Skill

__all__ = ["InsightStore", "Skill"]
