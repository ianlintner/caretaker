"""Tests for MongoEvolutionBackend and the evolution store factory."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from caretaker.evolution.insight_store import Mutation, Skill, _skill_id

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_skill_doc(
    category: str = "ci",
    signature: str = "jest_timeout",
    success_count: int = 5,
    fail_count: int = 1,
) -> dict:
    now = datetime.now(UTC)
    sid = _skill_id(category, signature)
    return {
        "_id": sid,
        "category": category,
        "signature": signature,
        "sop_text": "Increase testTimeout",
        "success_count": success_count,
        "fail_count": fail_count,
        "last_used_at": now,
        "created_at": now,
    }


def _make_mutation_doc(outcome: str | None = "pending") -> dict:
    now = datetime.now(UTC)
    return {
        "_id": "mut-001",
        "agent_name": "pr_agent",
        "parameter": "copilot_max_retries",
        "old_value": "2",
        "new_value": "3",
        "goal_id": "goal-pr-health",
        "goal_score_before": 0.6,
        "goal_score_after": None,
        "runs_evaluated": 0,
        "started_at": now,
        "ended_at": None,
        "outcome": outcome,
    }


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pymongo():
    """Patch pymongo.MongoClient so no real connection is opened."""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_skills_col = MagicMock()
    mock_mutations_col = MagicMock()

    mock_client.__getitem__.return_value = mock_db
    mock_db.__getitem__.side_effect = lambda name: (
        mock_skills_col if "skill" in name else mock_mutations_col
    )

    with (
        patch.dict(
            "sys.modules", {"pymongo": MagicMock(MongoClient=MagicMock(return_value=mock_client))}
        ),
        patch(
            "caretaker.evolution.backends.mongo_backend.MongoEvolutionBackend.__init__",
            autospec=True,
        ),
    ):
        yield mock_client, mock_skills_col, mock_mutations_col


@pytest.fixture
def backend():
    """MongoEvolutionBackend with fully mocked pymongo collections."""
    import pymongo  # noqa: F401 — may not be installed; mock below handles it

    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_skills_col = MagicMock()
    mock_mutations_col = MagicMock()
    mock_client.__getitem__.return_value = mock_db

    def _db_getitem(name: str):
        if "skill" in name:
            return mock_skills_col
        return mock_mutations_col

    mock_db.__getitem__.side_effect = _db_getitem

    mock_pymongo_module = MagicMock()
    mock_pymongo_module.MongoClient.return_value = mock_client
    mock_pymongo_module.ASCENDING = 1
    mock_pymongo_module.DESCENDING = -1

    with patch.dict("sys.modules", {"pymongo": mock_pymongo_module}):
        from caretaker.evolution.backends.mongo_backend import MongoEvolutionBackend

        b = MongoEvolutionBackend.__new__(MongoEvolutionBackend)
        b._client = mock_client
        b._skills = mock_skills_col
        b._mutations = mock_mutations_col
        return b, mock_skills_col, mock_mutations_col


# ── Skill upsert tests ─────────────────────────────────────────────────────────


class TestMongoEvolutionBackendSkills:
    def test_upsert_skill_success_calls_update_one(self, backend):
        b, skills_col, _ = backend
        sid = _skill_id("ci", "jest_timeout")
        b.upsert_skill_success(sid, "ci", "jest_timeout", "Increase testTimeout")

        skills_col.update_one.assert_called_once()
        args, kwargs = skills_col.update_one.call_args
        filter_doc, update_doc = args
        assert filter_doc == {"_id": sid}
        assert "$inc" in update_doc
        assert update_doc["$inc"] == {"success_count": 1}
        assert "$setOnInsert" in update_doc
        assert kwargs.get("upsert") is True

    def test_upsert_skill_failure_calls_update_one(self, backend):
        b, skills_col, _ = backend
        sid = _skill_id("ci", "jest_timeout")
        b.upsert_skill_failure(sid, "ci", "jest_timeout")

        skills_col.update_one.assert_called_once()
        args, _ = skills_col.update_one.call_args
        _, update_doc = args
        assert update_doc["$inc"] == {"fail_count": 1}
        assert update_doc["$setOnInsert"]["success_count"] == 0

    def test_query_skills_uses_category_and_expr(self, backend):
        b, skills_col, _ = backend
        doc = _make_skill_doc(success_count=5, fail_count=1)
        skills_col.find.return_value = [doc]

        results = b.query_skills("ci", min_attempts=3, limit=10)

        skills_col.find.assert_called_once()
        call_args = skills_col.find.call_args
        flt = call_args[0][0]
        assert flt["category"] == "ci"
        assert "$expr" in flt
        assert len(results) == 1
        assert results[0].success_count == 5

    def test_query_skills_python_confidence_sort(self, backend):
        b, skills_col, _ = backend
        low_doc = _make_skill_doc(success_count=3, fail_count=7)  # 0.3 confidence
        high_doc = _make_skill_doc(success_count=8, fail_count=2, signature="webpack_oom")  # 0.8
        skills_col.find.return_value = [low_doc, high_doc]

        results = b.query_skills("ci", min_attempts=3, limit=10)
        assert results[0].confidence >= results[1].confidence

    def test_get_skill_found(self, backend):
        b, skills_col, _ = backend
        doc = _make_skill_doc()
        skills_col.find_one.return_value = doc

        skill = b.get_skill(doc["_id"])
        assert skill is not None
        assert skill.category == "ci"

    def test_get_skill_missing_returns_none(self, backend):
        b, skills_col, _ = backend
        skills_col.find_one.return_value = None

        assert b.get_skill("nonexistent") is None

    def test_all_skills_no_filter(self, backend):
        b, skills_col, _ = backend
        skills_col.find.return_value = [_make_skill_doc()]

        results = b.all_skills()
        skills_col.find.assert_called_once_with({}, sort=[("success_count", -1)])
        assert len(results) == 1

    def test_all_skills_with_category(self, backend):
        b, skills_col, _ = backend
        skills_col.find.return_value = []

        b.all_skills(category="build")
        call_args = skills_col.find.call_args
        assert call_args[0][0] == {"category": "build"}

    def test_delete_skills_empty_list_returns_zero(self, backend):
        b, skills_col, _ = backend
        assert b.delete_skills([]) == 0
        skills_col.delete_many.assert_not_called()

    def test_delete_skills_returns_deleted_count(self, backend):
        b, skills_col, _ = backend
        skills_col.delete_many.return_value = MagicMock(deleted_count=2)

        count = b.delete_skills(["id1", "id2"])
        assert count == 2
        skills_col.delete_many.assert_called_once_with({"_id": {"$in": ["id1", "id2"]}})


# ── Mutation tests ─────────────────────────────────────────────────────────────


class TestMongoEvolutionBackendMutations:
    def _make_mutation(self, outcome: str | None = "pending") -> Mutation:
        return Mutation(
            id="mut-001",
            agent_name="pr_agent",
            parameter="copilot_max_retries",
            old_value="2",
            new_value="3",
            goal_id="goal-pr-health",
            goal_score_before=0.6,
            goal_score_after=None,
            runs_evaluated=0,
            started_at=datetime.now(UTC),
            ended_at=None,
            outcome=outcome,
        )

    def test_upsert_mutation_calls_replace_one(self, backend):
        b, _, mutations_col = backend
        mutation = self._make_mutation()
        b.upsert_mutation(mutation)

        mutations_col.replace_one.assert_called_once()
        args, kwargs = mutations_col.replace_one.call_args
        assert args[0] == {"_id": "mut-001"}
        assert args[1]["agent_name"] == "pr_agent"
        assert kwargs.get("upsert") is True

    def test_active_mutations_filters_pending_and_none(self, backend):
        b, _, mutations_col = backend
        mutations_col.find.return_value = [_make_mutation_doc("pending")]

        results = b.active_mutations()

        mutations_col.find.assert_called_once()
        flt = mutations_col.find.call_args[0][0]
        assert "$or" in flt
        assert len(results) == 1

    def test_mutation_history_sorts_desc(self, backend):
        b, _, mutations_col = backend
        mutations_col.find.return_value = [_make_mutation_doc("accepted")]

        b.mutation_history(limit=25)

        call_kwargs = mutations_col.find.call_args[1]
        assert call_kwargs.get("sort") == [("started_at", -1)]
        assert call_kwargs.get("limit") == 25

    def test_close_does_not_raise(self, backend):
        b, _, _ = backend
        b._client = MagicMock()
        b.close()
        b._client.close.assert_called_once()


# ── InsightStore facade over MongoEvolutionBackend ─────────────────────────────


class TestInsightStoreWithMongoBackend:
    def test_record_success_delegates_to_backend(self):
        mock_backend = MagicMock()
        from caretaker.evolution.insight_store import InsightStore

        store = InsightStore(db_path=":memory:", backend=mock_backend)
        store.record_success("ci", "jest_timeout", "Increase testTimeout")

        mock_backend.upsert_skill_success.assert_called_once()
        args = mock_backend.upsert_skill_success.call_args[0]
        assert args[1] == "ci"
        assert args[2] == "jest_timeout"

    def test_record_failure_delegates_to_backend(self):
        mock_backend = MagicMock()
        from caretaker.evolution.insight_store import InsightStore

        store = InsightStore(db_path=":memory:", backend=mock_backend)
        store.record_failure("ci", "jest_timeout")

        mock_backend.upsert_skill_failure.assert_called_once()

    def test_get_relevant_filters_by_confidence(self):
        mock_backend = MagicMock()
        now = datetime.now(UTC)
        low = Skill("ci:aaa", "ci", "sig1", "sop1", 1, 9, now, now)  # 0.1 confidence
        high = Skill("ci:bbb", "ci", "sig2", "sop2", 8, 2, now, now)  # 0.8 confidence
        mock_backend.query_skills.return_value = [high, low]

        from caretaker.evolution.insight_store import InsightStore

        store = InsightStore(db_path=":memory:", backend=mock_backend)
        results = store.get_relevant("ci", "any_sig", min_confidence=0.5)

        assert len(results) == 1
        assert results[0].confidence == 0.8

    def test_no_sqlite_connection_opened_with_backend(self):
        mock_backend = MagicMock()
        from caretaker.evolution.insight_store import InsightStore

        store = InsightStore(db_path=":memory:", backend=mock_backend)
        assert store._conn is None

    def test_close_delegates_to_backend(self):
        mock_backend = MagicMock()
        from caretaker.evolution.insight_store import InsightStore

        store = InsightStore(db_path=":memory:", backend=mock_backend)
        store.close()
        mock_backend.close.assert_called_once()


# ── Factory tests ──────────────────────────────────────────────────────────────


class TestBuildEvolutionStore:
    def _config(self, evolution_enabled=True, backend="sqlite", mongo_enabled=False):
        from caretaker.config import MaintainerConfig

        cfg = MaintainerConfig()
        cfg = cfg.model_copy(
            update={
                "evolution": cfg.evolution.model_copy(
                    update={"enabled": evolution_enabled, "backend": backend}
                ),
                "mongo": cfg.mongo.model_copy(update={"enabled": mongo_enabled}),
            }
        )
        return cfg

    def test_disabled_returns_none(self):
        from caretaker.evolution.backends.factory import build_evolution_store

        cfg = self._config(evolution_enabled=False)
        assert build_evolution_store(cfg) is None

    def test_sqlite_backend_returns_insight_store(self):
        from caretaker.evolution.backends.factory import build_evolution_store
        from caretaker.evolution.insight_store import InsightStore

        cfg = self._config(backend="sqlite")
        store = build_evolution_store(cfg)
        assert isinstance(store, InsightStore)
        store.close()

    def test_mongo_backend_without_mongo_enabled_falls_back(self):
        from caretaker.evolution.backends.factory import build_evolution_store
        from caretaker.evolution.insight_store import InsightStore

        cfg = self._config(backend="mongo", mongo_enabled=False)
        store = build_evolution_store(cfg)
        # Falls back to SQLite InsightStore
        assert isinstance(store, InsightStore)
        store.close()

    def test_mongo_backend_missing_url_falls_back(self):
        import os

        from caretaker.evolution.backends.factory import build_evolution_store
        from caretaker.evolution.insight_store import InsightStore

        cfg = self._config(backend="mongo", mongo_enabled=True)
        # Ensure env var is unset so build_mongo_evolution_backend raises RuntimeError
        os.environ.pop("MONGODB_URL", None)

        store = build_evolution_store(cfg)
        assert isinstance(store, InsightStore)
        store.close()

    def test_mongo_backend_wraps_in_insight_store(self):
        import os

        from caretaker.evolution.backends.factory import build_evolution_store
        from caretaker.evolution.insight_store import InsightStore

        cfg = self._config(backend="mongo", mongo_enabled=True)
        os.environ["MONGODB_URL"] = "mongodb://localhost:27017"

        mock_pymongo = MagicMock()
        mock_pymongo.ASCENDING = 1
        mock_pymongo.DESCENDING = -1
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_col = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_col
        mock_pymongo.MongoClient.return_value = mock_client

        with patch.dict("sys.modules", {"pymongo": mock_pymongo}):
            store = build_evolution_store(cfg)

        assert isinstance(store, InsightStore)
        # The backend is MongoEvolutionBackend (not _SQLiteEvolutionBackend)
        from caretaker.evolution.backends.mongo_backend import MongoEvolutionBackend

        assert isinstance(store._backend, MongoEvolutionBackend)
        os.environ.pop("MONGODB_URL", None)
