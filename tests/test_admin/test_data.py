"""Tests for the AdminDataAccess aggregations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from caretaker.admin.causal_store import CausalEventStore
from caretaker.admin.data import AdminDataAccess
from caretaker.causal_chain import CausalEvent, CausalEventRef
from caretaker.state.models import OrchestratorState, RunSummary, TrackedPR


def _run(
    offset_minutes: int = 0,
    self_heal_local: int = 0,
    self_heal_upstream_bugs: int = 0,
    self_heal_upstream_features: int = 0,
    prs_escalated: int = 0,
    issues_escalated: int = 0,
    stale_assignments_escalated: int = 0,
    escalation_rate: float = 0.0,
) -> RunSummary:
    return RunSummary(
        run_at=datetime.now(UTC) - timedelta(minutes=offset_minutes),
        self_heal_local_issues=self_heal_local,
        self_heal_upstream_bugs=self_heal_upstream_bugs,
        self_heal_upstream_features=self_heal_upstream_features,
        prs_escalated=prs_escalated,
        issues_escalated=issues_escalated,
        stale_assignments_escalated=stale_assignments_escalated,
        escalation_rate=escalation_rate,
    )


class TestStormMetrics:
    def test_empty_history_returns_zeros(self) -> None:
        data = AdminDataAccess(state=OrchestratorState())
        metrics = data.get_storm_metrics()
        assert metrics["window_runs"] == 0
        assert metrics["self_heal_total"] == 0
        assert metrics["escalations_total"] == 0

    def test_aggregates_self_heal_across_runs(self) -> None:
        state = OrchestratorState(
            run_history=[
                _run(offset_minutes=5, self_heal_local=3, self_heal_upstream_bugs=1),
                _run(offset_minutes=3, self_heal_local=10, self_heal_upstream_features=2),
                _run(offset_minutes=1, self_heal_local=0),
            ],
        )
        data = AdminDataAccess(state=state)

        metrics = data.get_storm_metrics()

        assert metrics["window_runs"] == 3
        assert metrics["self_heal_total"] == 3 + 1 + 10 + 2
        assert metrics["self_heal_max_single_run"] == 12

    def test_window_clips_to_most_recent(self) -> None:
        state = OrchestratorState(
            run_history=[
                _run(offset_minutes=50, self_heal_local=100),
                _run(offset_minutes=5, self_heal_local=1),
                _run(offset_minutes=1, self_heal_local=2),
            ],
        )
        data = AdminDataAccess(state=state)

        metrics = data.get_storm_metrics(window_runs=2)

        # Oldest (100) excluded by window.
        assert metrics["window_runs"] == 2
        assert metrics["self_heal_total"] == 3
        assert metrics["self_heal_max_single_run"] == 2

    def test_aggregates_escalations(self) -> None:
        state = OrchestratorState(
            run_history=[
                _run(prs_escalated=2, issues_escalated=1, escalation_rate=0.1),
                _run(prs_escalated=0, stale_assignments_escalated=3, escalation_rate=0.2),
            ],
        )
        data = AdminDataAccess(state=state)

        metrics = data.get_storm_metrics()

        assert metrics["escalations_total"] == 6
        assert metrics["avg_escalation_rate"] == 0.15


class TestFanoutMetrics:
    def test_no_prs_returns_zeros(self) -> None:
        data = AdminDataAccess(state=OrchestratorState())
        metrics = data.get_fanout_metrics()
        assert metrics["tracked_prs"] == 0
        assert metrics["hot_prs"] == []

    def test_identifies_hot_prs(self) -> None:
        state = OrchestratorState(
            tracked_prs={
                1: TrackedPR(number=1, fix_cycles=0, copilot_attempts=0),
                2: TrackedPR(number=2, fix_cycles=3, copilot_attempts=2),
                3: TrackedPR(number=3, fix_cycles=1, copilot_attempts=4),
                4: TrackedPR(number=4, fix_cycles=2, copilot_attempts=1),
            },
        )
        data = AdminDataAccess(state=state)

        metrics = data.get_fanout_metrics(high_cycle_threshold=2)

        assert metrics["tracked_prs"] == 4
        assert metrics["max_fix_cycles"] == 3
        assert metrics["max_copilot_attempts"] == 4
        assert metrics["high_cycle_prs"] == 2  # PRs 2 & 4 at threshold 2
        assert metrics["high_attempt_prs"] == 1  # PR 3 at attempts >= 3
        hot_numbers = [p["number"] for p in metrics["hot_prs"]]
        # PRs 2, 3, 4 are hot; PR 1 is cold.
        assert 1 not in hot_numbers
        assert set(hot_numbers) == {2, 3, 4}

    def test_hot_prs_capped_at_twenty(self) -> None:
        state = OrchestratorState(
            tracked_prs={i: TrackedPR(number=i, fix_cycles=5) for i in range(1, 30)},
        )
        data = AdminDataAccess(state=state)

        metrics = data.get_fanout_metrics()

        assert len(metrics["hot_prs"]) == 20


def _mk_event(
    event_id: str,
    *,
    parent: str | None = None,
    source: str = "devops",
    ref: CausalEventRef | None = None,
) -> CausalEvent:
    return CausalEvent(
        id=event_id,
        source=source,
        parent_id=parent,
        ref=ref or CausalEventRef(kind="issue", number=1),
    )


class TestCausalEndpoints:
    def _populated(self) -> AdminDataAccess:
        store = CausalEventStore()
        # a → b → c chain; d is sibling of b
        store.ingest(_mk_event("a", source="devops"))
        store.ingest(_mk_event("b", parent="a", source="issue-agent:dispatch"))
        store.ingest(_mk_event("c", parent="b", source="pr-agent-task"))
        store.ingest(_mk_event("d", parent="a", source="charlie:close-duplicate-pr"))
        return AdminDataAccess(causal_store=store)

    def test_list_events_returns_paged_items(self) -> None:
        data = self._populated()
        page = data.get_causal_events(limit=10)
        assert page.total == 4
        assert {item["id"] for item in page.items} == {"a", "b", "c", "d"}

    def test_list_events_filters_by_source(self) -> None:
        data = self._populated()
        page = data.get_causal_events(source="devops")
        assert page.total == 1
        assert page.items[0]["id"] == "a"

    def test_get_causal_chain_returns_root_first(self) -> None:
        data = self._populated()
        chain = data.get_causal_chain("c")
        assert chain is not None
        assert [e["id"] for e in chain["events"]] == ["a", "b", "c"]
        assert chain["truncated"] is False

    def test_get_causal_chain_unknown_returns_none(self) -> None:
        data = self._populated()
        assert data.get_causal_chain("missing") is None

    def test_get_descendants_returns_children(self) -> None:
        data = self._populated()
        descendants = data.get_causal_descendants("a")
        assert descendants is not None
        ids = {e["id"] for e in descendants["events"]}
        assert ids == {"b", "c", "d"}

    def test_get_descendants_unknown_returns_none(self) -> None:
        data = self._populated()
        assert data.get_causal_descendants("missing") is None

    def test_default_data_access_has_empty_store(self) -> None:
        data = AdminDataAccess()
        page = data.get_causal_events()
        assert page.total == 0
        assert page.items == []
