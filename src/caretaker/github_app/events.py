"""GitHub webhook event → caretaker agent name mapping.

This is a superset of ``caretaker.agents.EVENT_AGENT_MAP``.  The
orchestrator's map only covers the handful of events caretaker actually
polls for today; the App receives a wider set of webhook events (security
alerts, issue comments, workflow runs, …) and needs the additional
entries here to route them to the right agent.

Keeping this table in the ``github_app`` package rather than extending
the orchestrator's map means the webhook receiver and the CLI-driven
orchestrator can evolve independently — Phase 2 will migrate individual
agents from the CLI map to the App map one at a time.
"""

from __future__ import annotations

# Event → agent routing for the App webhook receiver.
#
# Keys are GitHub event names as they appear in the ``X-GitHub-Event``
# header.  Values are agent names (matching registered ``BaseAgent.name``
# values in ``caretaker.agents.AGENT_MODES``).
EVENT_AGENT_MAP: dict[str, list[str]] = {
    # PR lifecycle
    "pull_request": ["pr"],
    "pull_request_review": ["pr"],
    "pull_request_review_comment": ["pr"],
    # Check runs / CI
    "check_run": ["pr"],
    "check_suite": ["pr"],
    "status": ["pr"],
    # Workflow runs (drives DevOps + self-heal + PR agent)
    "workflow_run": ["devops", "self-heal", "pr"],
    # Issues
    "issues": ["issue"],
    "issue_comment": ["issue", "pr"],
    # Security
    "dependabot_alert": ["security"],
    "code_scanning_alert": ["security"],
    "secret_scanning_alert": ["security"],
    # Repository push (docs / changelog triggers)
    "push": ["docs"],
    # App lifecycle — handled by the webhook receiver itself, no agent
    "installation": [],
    "installation_repositories": [],
    # Ping is what GitHub sends to validate the webhook URL on create.
    "ping": [],
}


def normalize_event_name(raw: str) -> str:
    """Return a canonical lower-case event name with whitespace stripped."""
    return raw.strip().lower()


def agents_for_event(event_type: str) -> list[str]:
    """Return the ordered list of agent names that should handle ``event_type``.

    Returns an empty list for events that are intentionally not routed to
    any agent (e.g. App-lifecycle events, ``ping``).
    """
    return list(EVENT_AGENT_MAP.get(normalize_event_name(event_type), []))


__all__ = [
    "EVENT_AGENT_MAP",
    "agents_for_event",
    "normalize_event_name",
]
