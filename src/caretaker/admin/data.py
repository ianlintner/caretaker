"""Unified read-only data access layer for the admin dashboard.

Wraps existing stores (MemoryStore, InsightStore, AuditLog) and the
OrchestratorState with query/pagination helpers for the REST API.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from caretaker.admin.causal_store import CausalEventStore
from caretaker.agents._registry_data import AGENT_MODES, EVENT_AGENT_MAP
from caretaker.causal_chain import CausalEvent  # noqa: TC001 (runtime-used in helpers)
from caretaker.config import MaintainerConfig  # noqa: TC001 (runtime-used)
from caretaker.state.models import OrchestratorState  # noqa: TC001 (runtime-used)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    offset: int
    limit: int


class NamespaceSummary(BaseModel):
    namespace: str
    key_count: int


class MemoryEntry(BaseModel):
    namespace: str
    key: str
    value: str
    created_at: str | None = None
    updated_at: str | None = None
    expires_at: str | None = None


class AgentInfo(BaseModel):
    name: str
    modes: list[str]
    events: list[str]


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


class AdminDataAccess:
    """Read-only data access for the admin dashboard.

    Initialised with optional store references.  When a store is ``None``,
    the corresponding endpoints return empty/placeholder data.
    """

    def __init__(
        self,
        config: MaintainerConfig | None = None,
        state: OrchestratorState | None = None,
        memory_store: Any | None = None,  # MemoryStore
        insight_store: Any | None = None,  # InsightStore
        causal_store: CausalEventStore | None = None,
    ) -> None:
        self._config = config
        self._state = state or OrchestratorState()
        self._memory = memory_store
        self._insights = insight_store
        self._causal = causal_store or CausalEventStore()

    def set_state(self, state: OrchestratorState) -> None:
        """Update the cached orchestrator state (called after each run)."""
        self._state = state

    # ── Causal event store access ────────────────────────────────────────
    @property
    def causal_store(self) -> CausalEventStore:
        return self._causal

    # ── Insight (skill) store access ─────────────────────────────────────
    @property
    def insight_store(self) -> Any | None:
        """Return the optional :class:`InsightStore`. ``None`` when unset.

        Needed by the M6 fleet-graph sync to pull SOP text for the
        :GlobalSkill abstraction pass; all other admin endpoints reach
        through helper methods rather than this raw accessor.
        """
        return self._insights

    # ── Config access ────────────────────────────────────────────────────
    @property
    def config(self) -> MaintainerConfig | None:
        return self._config

    # ── Orchestrator State ────────────────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self._state.model_dump_json())
        return result

    def get_tracked_prs(
        self,
        state_filter: str | None = None,
        ownership_filter: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResponse:
        prs = list(self._state.tracked_prs.values())
        if state_filter:
            prs = [p for p in prs if p.state == state_filter]
        if ownership_filter:
            prs = [p for p in prs if p.ownership_state == ownership_filter]
        total = len(prs)
        items = [json.loads(p.model_dump_json()) for p in prs[offset : offset + limit]]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    def get_tracked_pr(self, number: int) -> dict[str, Any] | None:
        pr = self._state.tracked_prs.get(number)
        if pr is None:
            return None
        result: dict[str, Any] = json.loads(pr.model_dump_json())
        return result

    def get_tracked_issues(
        self,
        state_filter: str | None = None,
        classification_filter: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResponse:
        issues = list(self._state.tracked_issues.values())
        if state_filter:
            issues = [i for i in issues if i.state == state_filter]
        if classification_filter:
            issues = [i for i in issues if i.classification == classification_filter]
        total = len(issues)
        items = [json.loads(i.model_dump_json()) for i in issues[offset : offset + limit]]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    def get_tracked_issue(self, number: int) -> dict[str, Any] | None:
        issue = self._state.tracked_issues.get(number)
        if issue is None:
            return None
        result: dict[str, Any] = json.loads(issue.model_dump_json())
        return result

    def get_run_history(self, offset: int = 0, limit: int = 20) -> PaginatedResponse:
        runs = self._state.run_history
        total = len(runs)
        items = [json.loads(r.model_dump_json()) for r in runs[offset : offset + limit]]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    def get_latest_run(self) -> dict[str, Any] | None:
        if self._state.last_run is None:
            return None
        result: dict[str, Any] = json.loads(self._state.last_run.model_dump_json())
        return result

    def get_goal_history(self) -> dict[str, Any]:
        result: dict[str, list[dict[str, Any]]] = {}
        for goal_id, snapshots in self._state.goal_history.items():
            result[goal_id] = [json.loads(s.model_dump_json()) for s in snapshots]
        return result

    # ── Metrics ───────────────────────────────────────────────────────────

    def get_storm_metrics(self, window_runs: int = 20) -> dict[str, Any]:
        """Aggregate self-heal + escalation activity across the most recent runs.

        Used by the admin dashboard to surface storm-class regressions early —
        the 2026-04-14 incident opened 108 self-heal PRs in 90 minutes; a
        rolling rate across the last N runs would have flagged it near run #5.

        Counts come from ``RunSummary`` fields already persisted per run, so
        no new instrumentation is needed.
        """
        runs = self._state.run_history[-window_runs:] if self._state.run_history else []
        if not runs:
            return {
                "window_runs": 0,
                "self_heal_total": 0,
                "self_heal_max_single_run": 0,
                "escalations_total": 0,
                "avg_escalation_rate": 0.0,
                "run_window_start": None,
                "run_window_end": None,
            }
        self_heal_per_run = [
            r.self_heal_local_issues + r.self_heal_upstream_bugs + r.self_heal_upstream_features
            for r in runs
        ]
        escalations_total = sum(
            r.prs_escalated + r.issues_escalated + r.stale_assignments_escalated for r in runs
        )
        avg_esc = sum(r.escalation_rate for r in runs) / len(runs) if runs else 0.0
        return {
            "window_runs": len(runs),
            "self_heal_total": sum(self_heal_per_run),
            "self_heal_max_single_run": max(self_heal_per_run) if self_heal_per_run else 0,
            "escalations_total": escalations_total,
            "avg_escalation_rate": round(avg_esc, 4),
            "run_window_start": runs[0].run_at.isoformat() if runs[0].run_at else None,
            "run_window_end": runs[-1].run_at.isoformat() if runs[-1].run_at else None,
        }

    def get_fanout_metrics(self, high_cycle_threshold: int = 2) -> dict[str, Any]:
        """Per-PR proxies for caretaker comment fan-out.

        True comment counts would require fetching GitHub comment lists per
        PR on every refresh — expensive at scale. Instead, this surfaces
        signals already tracked on ``TrackedPR``:

        - ``fix_cycles`` — each cycle typically writes a status update +
          task comment, so a high value correlates with heavy fan-out.
        - ``copilot_attempts`` — same dynamic; each attempt spawns an
          ``@copilot`` task comment plus surrounding status edits.

        The admin UI can alert above ``high_cycle_threshold`` to catch
        F1/F2/F9-class regressions before users notice.
        """
        prs = list(self._state.tracked_prs.values())
        if not prs:
            return {
                "tracked_prs": 0,
                "high_cycle_prs": 0,
                "high_attempt_prs": 0,
                "max_fix_cycles": 0,
                "max_copilot_attempts": 0,
                "hot_prs": [],
            }
        max_cycles = max(p.fix_cycles for p in prs)
        max_attempts = max(p.copilot_attempts for p in prs)
        high_cycle = [p for p in prs if p.fix_cycles >= high_cycle_threshold]
        high_attempt = [p for p in prs if p.copilot_attempts >= high_cycle_threshold + 1]

        hot_set = {p.number: p for p in high_cycle}
        for p in high_attempt:
            hot_set[p.number] = p
        hot_sorted = sorted(
            hot_set.values(),
            key=lambda p: (p.fix_cycles, p.copilot_attempts),
            reverse=True,
        )[:20]

        return {
            "tracked_prs": len(prs),
            "high_cycle_prs": len(high_cycle),
            "high_attempt_prs": len(high_attempt),
            "max_fix_cycles": max_cycles,
            "max_copilot_attempts": max_attempts,
            "hot_prs": [
                {
                    "number": p.number,
                    "fix_cycles": p.fix_cycles,
                    "copilot_attempts": p.copilot_attempts,
                    "state": p.state,
                    "escalated": p.escalated,
                }
                for p in hot_sorted
            ],
        }

    # ── Attribution telemetry ─────────────────────────────────────────────

    def get_attribution_weekly(
        self,
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Weekly attribution rollup for the configured repo.

        Returns the count of PRs caretaker touched / merged / rescued /
        abandoned since ``since``, plus the average
        ``avg_time_to_merge_hours`` for PRs caretaker closed as merged.
        ``since`` is inclusive and defaults to seven days ago. PRs
        without a ``merged_at`` never contribute to the time-to-merge
        average (it's ``None`` when no caretaker merges landed).

        The implementation is intentionally stateless: it walks the
        in-memory ``OrchestratorState.tracked_prs`` rather than talking
        to Neo4j so the dashboard renders under typical single-repo
        pressure. Multi-repo aggregation happens at the fleet tier via
        the heartbeat payload.
        """
        cutoff = since if since is not None else datetime.now(UTC) - timedelta(days=7)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        prs = list(self._state.tracked_prs.values())

        # ``merged_at`` is the closest proxy we have for "activity window."
        # For PRs caretaker never merged, fall back to ``last_checked`` so
        # abandoned / rescued PRs still show up in the rollup for the
        # window they were last observed in.
        def _window_stamp(pr: Any) -> datetime | None:
            stamp = pr.merged_at or pr.last_checked or pr.first_seen_at
            if stamp is None:
                return None
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            return stamp  # type: ignore[no-any-return]

        in_window = [pr for pr in prs if (s := _window_stamp(pr)) is not None and s >= cutoff]

        touched = sum(1 for pr in in_window if pr.caretaker_touched)
        merged = sum(1 for pr in in_window if pr.caretaker_merged)
        rescued = sum(1 for pr in in_window if pr.operator_intervened)
        # "Abandoned" mirrors the metrics classifier — escalated without
        # a rescue. We compute it here rather than importing the
        # classifier to keep the admin surface insulated from the
        # in-process metrics module's side-effects.
        abandoned = sum(
            1 for pr in in_window if pr.state.value == "escalated" and not pr.operator_intervened
        )

        merged_prs = [
            pr for pr in in_window if pr.caretaker_merged and pr.merged_at and pr.first_seen_at
        ]
        ttm_hours: float | None = None
        if merged_prs:
            total = 0.0
            for pr in merged_prs:
                first = pr.first_seen_at
                merged_at = pr.merged_at
                if first is None or merged_at is None:
                    continue
                if first.tzinfo is None:
                    first = first.replace(tzinfo=UTC)
                if merged_at.tzinfo is None:
                    merged_at = merged_at.replace(tzinfo=UTC)
                total += (merged_at - first).total_seconds() / 3600.0
            ttm_hours = round(total / len(merged_prs), 2) if merged_prs else None

        return {
            "since": cutoff.isoformat(),
            "touched": touched,
            "merged": merged,
            "operator_rescued": rescued,
            "abandoned": abandoned,
            "avg_time_to_merge_hours": ttm_hours,
        }

    # ── Memory Store ──────────────────────────────────────────────────────

    def get_memory_namespaces(self) -> list[NamespaceSummary]:
        if self._memory is None:
            return []
        conn = self._memory._conn  # noqa: SLF001
        now = datetime.now(UTC).isoformat()
        rows = conn.execute(
            """
            SELECT namespace, COUNT(*) as cnt FROM memory
            WHERE expires_at IS NULL OR expires_at > ?
            GROUP BY namespace ORDER BY namespace
            """,
            (now,),
        ).fetchall()
        return [NamespaceSummary(namespace=r[0], key_count=r[1]) for r in rows]

    def get_memory_entries(
        self,
        namespace: str,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResponse:
        if self._memory is None:
            return PaginatedResponse(items=[], total=0, offset=offset, limit=limit)

        conn = self._memory._conn  # noqa: SLF001
        now = datetime.now(UTC).isoformat()

        total_row = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE namespace=? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (namespace, now),
        ).fetchone()
        total = total_row[0] if total_row else 0

        rows = conn.execute(
            """
            SELECT namespace, key, value, created_at, updated_at, expires_at
            FROM memory WHERE namespace=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY updated_at DESC LIMIT ? OFFSET ?
            """,
            (namespace, now, limit, offset),
        ).fetchall()

        items = [
            MemoryEntry(
                namespace=r[0],
                key=r[1],
                value=r[2],
                created_at=r[3],
                updated_at=r[4],
                expires_at=r[5],
            ).model_dump()
            for r in rows
        ]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    # ── Skills / Evolution ────────────────────────────────────────────────

    def get_skills(
        self,
        category: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResponse:
        if self._insights is None:
            return PaginatedResponse(items=[], total=0, offset=offset, limit=limit)

        if category:
            all_skills = self._insights.top_skills(category, limit=9999)
        else:
            all_skills = []
            for cat in ("ci", "issue", "build", "security"):
                all_skills.extend(self._insights.top_skills(cat, limit=9999))

        total = len(all_skills)
        page = all_skills[offset : offset + limit]
        items = [
            {
                "id": s.id,
                "category": s.category,
                "signature": s.signature,
                "sop_text": s.sop_text,
                "success_count": s.success_count,
                "fail_count": s.fail_count,
                "confidence": s.confidence,
                "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
                "created_at": s.created_at.isoformat(),
            }
            for s in page
        ]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        if self._insights is None:
            return None
        # Search across all categories
        for cat in ("ci", "issue", "build", "security"):
            for s in self._insights.top_skills(cat, limit=9999):
                if s.id == skill_id:
                    return {
                        "id": s.id,
                        "category": s.category,
                        "signature": s.signature,
                        "sop_text": s.sop_text,
                        "success_count": s.success_count,
                        "fail_count": s.fail_count,
                        "confidence": s.confidence,
                        "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
                        "created_at": s.created_at.isoformat(),
                    }
        return None

    def get_mutations(self, offset: int = 0, limit: int = 50) -> PaginatedResponse:
        if self._insights is None:
            return PaginatedResponse(items=[], total=0, offset=offset, limit=limit)

        active = self._insights.active_mutations()
        total = len(active)
        page = active[offset : offset + limit]
        items = [asdict(m) for m in page]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    # ── Agents ────────────────────────────────────────────────────────────

    def get_agents(self) -> list[AgentInfo]:
        agents = []
        for name, modes in AGENT_MODES.items():
            events = [evt for evt, agent_list in EVENT_AGENT_MAP.items() if name in agent_list]
            agents.append(AgentInfo(name=name, modes=sorted(modes), events=events))
        return agents

    # ── Causal events ─────────────────────────────────────────────────────

    def get_causal_events(
        self,
        source: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedResponse:
        """Page through observed causal events, most recent first."""
        events, total = self._causal.list_events(source=source, offset=offset, limit=limit)
        items = [_causal_event_to_dict(e) for e in events]
        return PaginatedResponse(items=items, total=total, offset=offset, limit=limit)

    def get_causal_chain(
        self,
        event_id: str,
        max_depth: int = 50,
    ) -> dict[str, Any] | None:
        """Walk the parent chain of ``event_id`` root-first."""
        if self._causal.get(event_id) is None:
            return None
        chain = self._causal.walk(event_id, max_depth=max_depth)
        return {
            "id": event_id,
            "events": [_causal_event_to_dict(e) for e in chain.events],
            "truncated": chain.truncated,
        }

    def get_causal_descendants(
        self,
        event_id: str,
        max_depth: int = 50,
    ) -> dict[str, Any] | None:
        """Return BFS descendants of ``event_id``."""
        if self._causal.get(event_id) is None:
            return None
        descendants = self._causal.descendants(event_id, max_depth=max_depth)
        return {
            "id": event_id,
            "events": [_causal_event_to_dict(e) for e in descendants],
        }

    # ── Config ────────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        """Return sanitised config (secrets redacted)."""
        if self._config is None:
            return {}

        data: dict[str, Any] = json.loads(self._config.model_dump_json())

        # Redact env var references that might contain secrets
        _redact_env_keys(data)
        return data


def _causal_event_to_dict(event: CausalEvent) -> dict[str, Any]:
    """Serialize a :class:`CausalEvent` to a JSON-safe dict."""
    return {
        "id": event.id,
        "source": event.source,
        "parent_id": event.parent_id,
        "run_id": event.run_id,
        "title": event.title,
        "observed_at": event.observed_at.isoformat() if event.observed_at else None,
        "ref": {
            "kind": event.ref.kind,
            "number": event.ref.number,
            "comment_id": event.ref.comment_id,
            "owner": event.ref.owner,
            "repo": event.ref.repo,
        },
    }


def _redact_env_keys(obj: Any) -> None:
    """Recursively redact values for keys ending in '_env' or containing 'secret'."""
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if isinstance(obj[key], str) and (
                key.endswith("_env") or "secret" in key.lower() or "private_key" in key.lower()
            ):
                obj[key] = "***REDACTED***"
            else:
                _redact_env_keys(obj[key])
    elif isinstance(obj, list):
        for item in obj:
            _redact_env_keys(item)
