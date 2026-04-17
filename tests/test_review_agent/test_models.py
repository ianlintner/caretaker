from datetime import datetime

from caretaker.review_agent.models import (
    DimensionScore,
    EvidenceCounters,
    Findings,
    OutputManifest,
    OverallScore,
    Retrospective,
    ReviewDimensions,
    ReviewReport,
    ReviewRequest,
    ReviewScorecard,
    TargetInfo,
    WindowInfo,
)


def test_target_info():
    t1 = TargetInfo(kind="run", number=123, title="Run 123", url="http://example.com/run/123")
    assert t1.kind == "run"
    assert t1.number == 123

    t2 = TargetInfo(kind="pull_request")
    assert t2.kind == "pull_request"
    assert t2.number is None


def test_review_scorecard():
    scorecard = ReviewScorecard(
        reviewed_at=datetime.utcnow(),
        target=TargetInfo(kind="run"),
        window=WindowInfo(lookback_runs=5, lookback_days=7),
        overall=OverallScore(score=95, grade="A", confidence=0.9, status="excellent"),
        dimensions=ReviewDimensions(
            outcome=DimensionScore(score=100, weight=0.3),
            execution=DimensionScore(score=90, weight=0.2),
            reliability=DimensionScore(score=95, weight=0.2),
            maintainability=DimensionScore(score=90, weight=0.15),
            communication=DimensionScore(score=90, weight=0.15),
        ),
        findings=Findings(strengths=["Good performance"]),
        retro=Retrospective(went_well=["Smooth run"]),
        evidence=EvidenceCounters(),
        outputs=OutputManifest(),
    )
    assert scorecard.agent == "review"
    assert scorecard.schema_version == "v1"
    assert scorecard.overall.score == 95
    assert scorecard.findings.strengths == ["Good performance"]


def test_review_report():
    report = ReviewReport()
    assert report.reviews_completed == 0
    assert report.artifacts_written == 0
    assert report.average_score == 0.0

    report.reviews_completed = 1
    report.average_score = 90.0
    assert report.average_score == 90.0


def test_review_request():
    req = ReviewRequest(target_kind="run", lookback_runs=5)
    assert req.target_kind == "run"
    assert req.lookback_runs == 5
    assert req.lookback_days == 30  # default
