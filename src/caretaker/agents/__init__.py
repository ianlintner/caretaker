"""Agent adapters and registry wiring.

Re-exports the public API previously provided by the ``agents`` module
so that ``from caretaker.agents import X`` keeps working.
"""

from __future__ import annotations

from caretaker.agents._registry_data import (
    AGENT_MODES,
    ALL_ADAPTERS,
    EVENT_AGENT_MAP,
    build_registry,
)
from caretaker.charlie_agent.adapter import CharlieAgentAdapter
from caretaker.dependency_agent.adapter import DependencyAgentAdapter
from caretaker.devops_agent.adapter import DevOpsAgentAdapter
from caretaker.docs_agent.adapter import DocsAgentAdapter
from caretaker.escalation_agent.adapter import EscalationAgentAdapter
from caretaker.issue_agent.adapter import IssueAgentAdapter
from caretaker.migration_agent.agent import MigrationAgent
from caretaker.perf_agent.agent import PerformanceAgent
from caretaker.pr_agent.adapter import PRAgentAdapter
from caretaker.pr_agent.triage_adapter import TriageAgentAdapter
from caretaker.pr_ci_approver.agent import PRCIApproverAgent
from caretaker.pr_reviewer.agent import PRReviewerAgent
from caretaker.principal_agent.agent import PrincipalAgent
from caretaker.refactor_agent.agent import RefactorAgent
from caretaker.review_agent.agent import ReviewAgent
from caretaker.security_agent.adapter import SecurityAgentAdapter
from caretaker.self_heal_agent.adapter import SelfHealAgentAdapter
from caretaker.stale_agent.adapter import StaleAgentAdapter
from caretaker.test_agent.agent import TestAgent
from caretaker.upgrade_agent.adapter import UpgradeAgentAdapter

__all__ = [
    "ALL_ADAPTERS",
    "AGENT_MODES",
    "EVENT_AGENT_MAP",
    "build_registry",
    "PRAgentAdapter",
    "IssueAgentAdapter",
    "UpgradeAgentAdapter",
    "DevOpsAgentAdapter",
    "SelfHealAgentAdapter",
    "SecurityAgentAdapter",
    "DependencyAgentAdapter",
    "DocsAgentAdapter",
    "CharlieAgentAdapter",
    "StaleAgentAdapter",
    "EscalationAgentAdapter",
    "ReviewAgent",
    "PrincipalAgent",
    "TestAgent",
    "RefactorAgent",
    "PerformanceAgent",
    "MigrationAgent",
    "PRReviewerAgent",
    "PRCIApproverAgent",
    "TriageAgentAdapter",
]
