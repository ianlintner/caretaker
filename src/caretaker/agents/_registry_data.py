"""Central registry wiring — imports all agent adapters and defines registries."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
from caretaker.pr_reviewer.agent import PRReviewerAgent
from caretaker.principal_agent.agent import PrincipalAgent
from caretaker.refactor_agent.agent import RefactorAgent
from caretaker.review_agent.agent import ReviewAgent
from caretaker.security_agent.adapter import SecurityAgentAdapter
from caretaker.self_heal_agent.adapter import SelfHealAgentAdapter
from caretaker.stale_agent.adapter import StaleAgentAdapter
from caretaker.test_agent.agent import TestAgent
from caretaker.upgrade_agent.adapter import UpgradeAgentAdapter

if TYPE_CHECKING:
    from caretaker.agent_protocol import AgentContext, BaseAgent
    from caretaker.registry import AgentRegistry

ALL_ADAPTERS: list[type[BaseAgent]] = [
    PRAgentAdapter,
    IssueAgentAdapter,
    UpgradeAgentAdapter,
    DevOpsAgentAdapter,
    SelfHealAgentAdapter,
    SecurityAgentAdapter,
    DependencyAgentAdapter,
    DocsAgentAdapter,
    CharlieAgentAdapter,
    StaleAgentAdapter,
    EscalationAgentAdapter,
    ReviewAgent,
    PrincipalAgent,
    TestAgent,
    RefactorAgent,
    PerformanceAgent,
    MigrationAgent,
    PRReviewerAgent,
    TriageAgentAdapter,
]

# Maps agent name -> set of run modes that include it
AGENT_MODES: dict[str, set[str]] = {
    "pr": {"full", "pr-only"},
    "issue": {"full", "issue-only"},
    "upgrade": {"full", "upgrade"},
    "devops": {"full", "devops"},
    "self-heal": {"self-heal"},  # scheduled self-heal mode only
    "security": {"full", "security"},
    "deps": {"full", "deps"},
    "docs": {"full", "docs"},
    "charlie": {"full", "charlie"},
    "stale": {"full", "stale"},
    "escalation": {"full", "escalation"},
    "review": {"full"},
    "principal": {"full", "principal"},
    "test": {"full", "test"},
    "refactor": {"full", "refactor"},
    "perf": {"full", "perf"},
    "migration": {"full", "migration"},
    "pr-reviewer": {"full", "pr-only", "pr-reviewer"},
    "triage": {"full", "triage"},
}

# Maps GitHub event types -> list of agent names to run
EVENT_AGENT_MAP: dict[str, list[str]] = {
    "pull_request": ["pr", "pr-reviewer", "principal", "test", "perf"],
    "pull_request_review": ["pr"],
    "check_run": ["pr"],
    "check_suite": ["pr"],
    "issues": ["issue", "principal"],
    "issue_comment": ["issue"],
    "workflow_run": ["devops", "self-heal", "pr"],
    "dependabot_alert": ["security"],
}


def build_registry(ctx: AgentContext) -> AgentRegistry:
    """Construct a fully populated AgentRegistry from the given context."""
    from caretaker.registry import AgentRegistry

    registry = AgentRegistry()
    for adapter_cls in ALL_ADAPTERS:
        agent = adapter_cls(ctx)
        modes = AGENT_MODES.get(agent.name, {"full"})
        registry.register(agent, modes=modes)
    return registry
