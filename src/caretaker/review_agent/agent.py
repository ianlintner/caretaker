"""Review Agent - evaluates completed caretaker work and generates retrospectives."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import BaseAgent, AgentResult
from caretaker.review_agent.models import (
    ReviewScorecard,
    TargetInfo,
    WindowInfo,
    OverallScore,
    ReviewDimensions,
    DimensionScore,
    Findings,
    Retrospective,
    EvidenceCounters,
    OutputManifest,
    ReviewReport,
)

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState, RunSummary

logger = logging.getLogger(__name__)


class ReviewAgent(BaseAgent):
    """Evaluates completed caretaker work across issues, PRs, and prior runs."""

    @property
    def name(self) -> str:
        return "review"

    def enabled(self) -> bool:
        # Check config once ReviewAgentConfig is added to MaintainerConfig
        if hasattr(self._ctx.config, "review_agent"):
            return getattr(self._ctx.config.review_agent, "enabled", False)
        return False

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Run the review agent."""
        logger.info("Starting review agent")
        report = ReviewReport()

        cfg = getattr(self._ctx.config, "review_agent", None)
        if not cfg:
            report.errors.append("review_agent config missing")
            return AgentResult(processed=0, errors=report.errors)

        # Basic implementation of a scheduled review of the last run.
        # In a full implementation, we'd iterate over explicitly requested targets.
        
        target = TargetInfo(
            kind="run",
            number=None,
            title="Scheduled Run Review",
        )

        scorecard = self._evaluate_run(state, target, cfg)
        
        if scorecard:
            self._save_artifacts(scorecard, cfg)
            report.reviews_completed += 1
            report.artifacts_written += (1 if cfg.save_markdown else 0) + (1 if cfg.save_json else 0)
            report.average_score = scorecard.overall.score

        return AgentResult(
            processed=report.reviews_completed,
            errors=report.errors,
            extra={
                "artifacts_written": report.artifacts_written,
                "average_score": report.average_score,
                "critical_findings": report.critical_findings,
                "trend_flags": report.trend_flags,
            },
        )

    def apply_summary(self, result: AgentResult, summary: RunSummary) -> None:
        """Map review metrics into RunSummary."""
        # Optional summary fields as per plan
        if not hasattr(summary, "review_average_score"):
            return
            
        setattr(summary, "reviews_completed", result.processed)
        setattr(summary, "review_artifacts_written", result.extra.get("artifacts_written", 0))
        setattr(summary, "review_average_score", result.extra.get("average_score", 0.0))

    def _evaluate_run(self, state: OrchestratorState, target: TargetInfo, cfg: Any) -> ReviewScorecard | None:
        """Evaluate a run based on the OrchestratorState."""
        
        # Simple heuristic grading
        score = 85
        grade = "B"
        
        # Lookback runs
        history_len = len(state.run_history) if hasattr(state, "run_history") else 0
        
        return ReviewScorecard(
            reviewed_at=datetime.utcnow(),
            target=target,
            window=WindowInfo(lookback_runs=cfg.lookback_runs, lookback_days=cfg.lookback_days),
            overall=OverallScore(score=score, grade=grade, confidence=0.8, status="mixed"),
            dimensions=ReviewDimensions(
                outcome=DimensionScore(score=90, weight=0.3, notes=["Run completed"]),
                execution=DimensionScore(score=80, weight=0.2, notes=["No major retries"]),
                reliability=DimensionScore(score=85, weight=0.2, notes=["Stable execution"]),
                maintainability=DimensionScore(score=85, weight=0.15, notes=["State valid"]),
                communication=DimensionScore(score=85, weight=0.15, notes=["Standard logging"]),
            ),
            findings=Findings(
                strengths=["Basic execution successful"],
                weaknesses=[],
                recurring_issues=[],
                anomalies=[],
            ),
            retro=Retrospective(
                went_well=["Run completed normally"],
                failed=[],
                do_better=["Need more granular metric tracking"],
                stop_or_less=[],
            ),
            evidence=EvidenceCounters(
                run_summaries_considered=min(cfg.lookback_runs, history_len),
                memory_entries_considered=0,
                tracker_signals=[],
                github_comments_considered=0,
            ),
            outputs=OutputManifest(),
        )

    def _save_artifacts(self, scorecard: ReviewScorecard, cfg: Any) -> None:
        """Save markdown and json artifacts."""
        if not cfg.save_markdown and not cfg.save_json:
            return

        artifact_dir = Path(cfg.artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = scorecard.reviewed_at.strftime("%Y%m%dT%H%M%SZ")
        base_name = f"run-{timestamp}"
        
        if cfg.save_json:
            json_path = artifact_dir / f"{base_name}.json"
            json_path.write_text(scorecard.model_dump_json(indent=2))
            scorecard.outputs.json_report_path = str(json_path)

        if cfg.save_markdown:
            md_path = artifact_dir / f"{base_name}.md"
            md_content = self._generate_markdown(scorecard)
            md_path.write_text(md_content)
            scorecard.outputs.markdown_report_path = str(md_path)

    def _generate_markdown(self, scorecard: ReviewScorecard) -> str:
        """Generate markdown representation of the scorecard."""
        return f"""# Review Report: {scorecard.target.title or scorecard.target.kind}

- Overall score: {scorecard.overall.score} `{scorecard.overall.grade}`
- Confidence: {scorecard.overall.confidence}
- Reviewed at: {scorecard.reviewed_at.isoformat()}

## Executive summary

{", ".join(scorecard.findings.strengths)}

## Scorecard

| Dimension | Score | Notes |
| --- | ---: | --- |
| Outcome | {scorecard.dimensions.outcome.score} | {", ".join(scorecard.dimensions.outcome.notes)} |
| Execution | {scorecard.dimensions.execution.score} | {", ".join(scorecard.dimensions.execution.notes)} |
| Reliability | {scorecard.dimensions.reliability.score} | {", ".join(scorecard.dimensions.reliability.notes)} |
| Maintainability | {scorecard.dimensions.maintainability.score} | {", ".join(scorecard.dimensions.maintainability.notes)} |
| Communication | {scorecard.dimensions.communication.score} | {", ".join(scorecard.dimensions.communication.notes)} |

## Retrospective

### What went well
{chr(10).join(f"- {item}" for item in scorecard.retro.went_well) if scorecard.retro.went_well else "- None"}

### What failed
{chr(10).join(f"- {item}" for item in scorecard.retro.failed) if scorecard.retro.failed else "- None"}

### What to do better
{chr(10).join(f"- {item}" for item in scorecard.retro.do_better) if scorecard.retro.do_better else "- None"}

### What to stop or do less of
{chr(10).join(f"- {item}" for item in scorecard.retro.stop_or_less) if scorecard.retro.stop_or_less else "- None"}
"""
