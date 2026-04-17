"""Tests for the webhook event → agent name mapping."""

from __future__ import annotations

from caretaker.github_app.events import (
    EVENT_AGENT_MAP,
    agents_for_event,
    normalize_event_name,
)


def test_pull_request_routes_to_pr_agent() -> None:
    assert agents_for_event("pull_request") == ["pr"]


def test_workflow_run_routes_to_devops_self_heal_and_pr() -> None:
    assert agents_for_event("workflow_run") == ["devops", "self-heal", "pr"]


def test_dependabot_alert_routes_to_security() -> None:
    assert agents_for_event("dependabot_alert") == ["security"]


def test_code_scanning_alert_routes_to_security() -> None:
    assert agents_for_event("code_scanning_alert") == ["security"]


def test_secret_scanning_alert_routes_to_security() -> None:
    assert agents_for_event("secret_scanning_alert") == ["security"]


def test_ping_is_intentionally_unrouted() -> None:
    assert agents_for_event("ping") == []


def test_installation_events_are_intentionally_unrouted() -> None:
    assert agents_for_event("installation") == []
    assert agents_for_event("installation_repositories") == []


def test_unknown_event_returns_empty_list() -> None:
    assert agents_for_event("not_a_real_event") == []


def test_event_names_are_normalized() -> None:
    assert agents_for_event("  Pull_Request ") == ["pr"]
    assert normalize_event_name("  PUSH\n") == "push"


def test_mapping_is_not_aliased_across_calls() -> None:
    first = agents_for_event("pull_request")
    first.append("mutated")
    assert agents_for_event("pull_request") == ["pr"]


def test_map_is_well_formed() -> None:
    # Every value is a list[str]; no empty-string event names.
    for event, agents in EVENT_AGENT_MAP.items():
        assert event
        assert event == normalize_event_name(event)
        assert isinstance(agents, list)
        assert all(isinstance(a, str) and a for a in agents)
