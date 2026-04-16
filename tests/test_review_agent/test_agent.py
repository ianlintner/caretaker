import json
from unittest.mock import MagicMock

import pytest

from caretaker.agent_protocol import AgentResult
from caretaker.review_agent.agent import ReviewAgent
from caretaker.review_agent.models import TargetInfo
from caretaker.state.models import OrchestratorState


class MockConfig:
    pass


class MockContext:
    def __init__(self, config):
        self.config = config


def make_agent(config=None):
    if config is None:
        config = MockConfig()
    ctx = MockContext(config)
    agent = ReviewAgent(ctx=ctx)
    return agent, config


def test_agent_name():
    agent, _ = make_agent()
    assert agent.name == "review"


def test_agent_enabled_false_no_config():
    agent, _ = make_agent()
    assert not agent.enabled()


def test_agent_enabled_false():
    agent, config = make_agent()
    config.review_agent = MagicMock(enabled=False)
    assert not agent.enabled()


def test_agent_enabled_true():
    agent, config = make_agent()
    config.review_agent = MagicMock(enabled=True)
    assert agent.enabled()


@pytest.mark.asyncio
async def test_execute_missing_config():
    agent, config = make_agent()
    state = OrchestratorState()
    result = await agent.execute(state)

    assert result.processed == 0
    assert "review_agent config missing" in result.errors


@pytest.mark.asyncio
async def test_execute_with_config(tmp_path):
    agent, config = make_agent()

    # Setup mock config
    config.review_agent = MagicMock(
        enabled=True,
        lookback_runs=5,
        lookback_days=30,
        save_json=True,
        save_markdown=True,
        artifact_dir=str(tmp_path / "artifacts"),
    )

    state = OrchestratorState()
    state.run_history = [{"id": 1}, {"id": 2}]

    result = await agent.execute(state)

    assert result.processed == 1
    assert result.errors == []
    assert result.extra["artifacts_written"] == 2
    assert result.extra["average_score"] == 85.0

    # Verify files written
    artifact_dir = tmp_path / "artifacts"
    assert artifact_dir.exists()

    files = list(artifact_dir.iterdir())
    assert len(files) == 2

    json_files = [f for f in files if f.suffix == ".json"]
    md_files = [f for f in files if f.suffix == ".md"]

    assert len(json_files) == 1
    assert len(md_files) == 1

    # Check JSON structure
    data = json.loads(json_files[0].read_text())
    assert data["agent"] == "review"
    assert data["schema_version"] == "v1"
    assert data["overall"]["score"] == 85
    assert data["evidence"]["run_summaries_considered"] == 2
    # output paths must be recorded inside the JSON artifact itself
    assert data["outputs"]["json_report_path"] is not None
    assert data["outputs"]["markdown_report_path"] is not None

    # Check Markdown content
    md_content = md_files[0].read_text()
    assert "# Review Report: Scheduled Run Review" in md_content
    assert "Overall score: 85 `B`" in md_content


class MockRunSummary:
    def __init__(self):
        self.mode = "event"
        self.review_average_score = 0.0


def test_apply_summary():
    agent, _ = make_agent()

    result = AgentResult(
        processed=2,
        errors=[],
        extra={
            "artifacts_written": 4,
            "average_score": 90.0,
        },
    )

    summary = MockRunSummary()

    agent.apply_summary(result, summary)

    assert summary.reviews_completed == 2
    assert summary.review_artifacts_written == 4
    assert summary.review_average_score == 90.0


def test_apply_summary_no_attributes():
    # apply_summary should return without crashing when summary lacks expected attributes

    agent, _ = make_agent()

    result = AgentResult(
        processed=2,
        errors=[],
        extra={
            "artifacts_written": 4,
            "average_score": 90.0,
        },
    )

    class MockRunSummaryNoAttr:
        pass

    summary = MockRunSummaryNoAttr()
    agent.apply_summary(result, summary)

    # Should just return without setting
    assert not hasattr(summary, "reviews_completed")


def test_save_artifacts_disabled(tmp_path):
    agent, config = make_agent()
    config.review_agent = MagicMock(
        save_json=False,
        save_markdown=False,
        lookback_runs=5,
        lookback_days=30,
        artifact_dir=str(tmp_path / "artifacts"),
    )

    scorecard = agent._evaluate_run(
        OrchestratorState(), TargetInfo(kind="run", title="Test"), config.review_agent
    )

    agent._save_artifacts(scorecard, config.review_agent)

    assert not (tmp_path / "artifacts").exists()
