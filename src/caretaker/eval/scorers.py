"""Per-site scorers over shadow-decision verdict pairs.

Each scorer consumes the two JSON blobs persisted on a
:class:`~caretaker.evolution.shadow.ShadowDecisionRecord`
(``legacy_verdict_json`` and ``candidate_verdict_json``) and returns a
:class:`ScorerResult` — a float in ``[0.0, 1.0]`` plus an optional
human-readable reason when the scorer disagrees.

Design notes:

* The scorers operate on **parsed JSON dicts**, not on the pydantic
  classes they came from. That keeps the eval harness independent of
  whichever verdict schema happens to be in-tree: if a field is
  renamed or removed, scorers degrade gracefully (``missing`` → ``0.0``
  with a diagnostic reason) rather than crashing the nightly run.
* One scorer per migrated decision site, plus a single LLM-judge
  scorer for the readiness ``summary`` free-text field. The judge
  model must be a different provider family from the candidate —
  pass one in explicitly at :class:`LLMJudge` construction so callers
  can't silently correlate.
* :data:`DEFAULT_SCORER_REGISTRY` maps decision-site name → tuple of
  scorers so the harness can look them up without inventing a
  registration protocol.

All scorers are **pure synchronous functions of two strings**. That is
deliberately the lowest-common-denominator shape: it lets tests seed
fixtures as raw JSON strings (the same shape Neo4j hands back) and it
makes the harness trivially parallelisable later.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScorerResult:
    """A scorer's output over one verdict pair.

    ``score`` in ``[0.0, 1.0]`` — the harness averages these for the
    per-site agreement rate. ``reason`` is an optional short diagnostic
    shown on the admin UI and logged when the scorer falls below 1.0.
    """

    score: float
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Clamp — scorers occasionally return floats just outside the
        # range (e.g. cosine similarity with float noise) and we would
        # rather silently clamp than blow up the nightly report.
        if not math.isfinite(self.score) or self.score < 0.0:
            object.__setattr__(self, "score", 0.0)
        elif self.score > 1.0:
            object.__setattr__(self, "score", 1.0)


# ── Scorer protocol ──────────────────────────────────────────────────────


@runtime_checkable
class Scorer(Protocol):
    """Callable that grades one shadow-decision record."""

    __name__: str

    def __call__(
        self,
        legacy_verdict_json: str,
        candidate_verdict_json: str | None,
    ) -> ScorerResult: ...


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse(blob: str | None) -> dict[str, Any] | None:
    """Parse a verdict JSON blob. ``None``/``""`` → ``None``."""
    if not blob:
        return None
    try:
        value = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    # Some verdicts are raw scalars (e.g. a single string path). Wrap so
    # scorers have a uniform dict to inspect.
    return {"__value__": value}


def _missing(reason: str) -> ScorerResult:
    """Both sides present but the scorer cannot inspect the field."""
    return ScorerResult(score=0.0, reason=reason)


def _degenerate_when(candidate_is_none: bool) -> ScorerResult | None:
    """Return a canonical ``ScorerResult`` for candidate_error rows."""
    if candidate_is_none:
        return ScorerResult(
            score=0.0,
            reason="candidate_error: no candidate verdict to score",
            metadata={"candidate_error": True},
        )
    return None


def _get(d: dict[str, Any] | None, *keys: str) -> Any:
    """Walk nested keys; return ``None`` on any missing step."""
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def _exact(
    legacy_json: str,
    candidate_json: str | None,
    *,
    fields: Sequence[str],
    reason_prefix: str,
) -> ScorerResult:
    """Exact-match helper: all ``fields`` must be equal between sides."""
    degenerate = _degenerate_when(candidate_json is None)
    if degenerate is not None:
        return degenerate
    legacy = _parse(legacy_json)
    candidate = _parse(candidate_json)
    if legacy is None or candidate is None:
        return _missing(f"{reason_prefix}: verdict blob not JSON")
    diffs: list[str] = []
    for f in fields:
        lv = legacy.get(f)
        cv = candidate.get(f)
        if lv != cv:
            diffs.append(f"{f}: legacy={lv!r} != candidate={cv!r}")
    if not diffs:
        return ScorerResult(score=1.0)
    return ScorerResult(score=0.0, reason="; ".join(diffs))


def _cosine_similarity(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Token-wise cosine similarity over two label lists.

    Treats the two lists as bags of tokens; equivalent to Jaccard-like
    cosine on the symmetric multiset vectors. Labels outside a known
    vocabulary are still counted — we just want "are these two label
    sets close?", not semantic similarity.
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    a_tokens = [str(x) for x in a]
    b_tokens = [str(x) for x in b]
    vocab = set(a_tokens) | set(b_tokens)
    va = [a_tokens.count(t) for t in vocab]
    vb = [b_tokens.count(t) for t in vocab]
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(x * x for x in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ── Site scorers ─────────────────────────────────────────────────────────


def readiness_verdict_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on :class:`~caretaker.pr_agent.readiness_llm.Readiness.verdict`."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("verdict",),
        reason_prefix="readiness_verdict_match",
    )


def ci_triage_category_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on ``category`` AND ``is_transient`` for CI-triage verdicts."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("category", "is_transient"),
        reason_prefix="ci_triage_category_match",
    )


def issue_triage_kind_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Issue-triage: exact on ``kind``; cosine ≥0.8 on ``suggested_labels``.

    The two checks combine: if ``kind`` disagrees the row scores 0.0;
    otherwise the score is the cosine similarity of the suggested-label
    lists, thresholded at 0.8 → 1.0 and 0.0 otherwise so the per-site
    agreement rate stays interpretable as a fraction.
    """
    degenerate = _degenerate_when(candidate_verdict_json is None)
    if degenerate is not None:
        return degenerate
    legacy = _parse(legacy_verdict_json)
    candidate = _parse(candidate_verdict_json)
    if legacy is None or candidate is None:
        return _missing("issue_triage_kind_match: verdict blob not JSON")

    legacy_kind = legacy.get("kind")
    candidate_kind = candidate.get("kind")
    if legacy_kind != candidate_kind:
        return ScorerResult(
            score=0.0,
            reason=f"kind: legacy={legacy_kind!r} != candidate={candidate_kind!r}",
        )

    legacy_labels = legacy.get("suggested_labels") or []
    candidate_labels = candidate.get("suggested_labels") or []
    if not isinstance(legacy_labels, list) or not isinstance(candidate_labels, list):
        return _missing("issue_triage_kind_match: suggested_labels not a list")

    cos = _cosine_similarity(legacy_labels, candidate_labels)
    if cos >= 0.8:
        return ScorerResult(
            score=1.0,
            metadata={"suggested_labels_cosine": cos},
        )
    return ScorerResult(
        score=0.0,
        reason=(
            f"suggested_labels cosine {cos:.3f} < 0.8 "
            f"(legacy={sorted(legacy_labels)!r}, candidate={sorted(candidate_labels)!r})"
        ),
        metadata={"suggested_labels_cosine": cos},
    )


def dispatch_guard_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on ``(is_self_echo, is_human_intent)`` tuple."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("is_self_echo", "is_human_intent"),
        reason_prefix="dispatch_guard_match",
    )


def review_classification_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on ``(kind, severity)`` for review classifications."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("kind", "severity"),
        reason_prefix="review_classification_match",
    )


def cascade_action_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on the cascade ``action`` vocabulary."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("action",),
        reason_prefix="cascade_action_match",
    )


def stuck_pr_match(legacy_verdict_json: str, candidate_verdict_json: str | None) -> ScorerResult:
    """Exact match on ``(is_stuck, recommended_action)``."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("is_stuck", "recommended_action"),
        reason_prefix="stuck_pr_match",
    )


def bot_identity_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on ``is_automated`` for bot-identity verdicts."""
    return _exact(
        legacy_verdict_json,
        candidate_verdict_json,
        fields=("is_automated",),
        reason_prefix="bot_identity_match",
    )


def executor_routing_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on the routing ``path`` field."""
    degenerate = _degenerate_when(candidate_verdict_json is None)
    if degenerate is not None:
        return degenerate
    legacy = _parse(legacy_verdict_json)
    candidate = _parse(candidate_verdict_json)
    if legacy is None or candidate is None:
        return _missing("executor_routing_match: verdict blob not JSON")
    lv = legacy.get("path") or legacy.get("__value__")
    cv = candidate.get("path") or candidate.get("__value__")
    if lv == cv:
        return ScorerResult(score=1.0)
    return ScorerResult(score=0.0, reason=f"path: legacy={lv!r} != candidate={cv!r}")


def crystallizer_category_match(
    legacy_verdict_json: str, candidate_verdict_json: str | None
) -> ScorerResult:
    """Exact match on the crystallizer-mapped category."""
    degenerate = _degenerate_when(candidate_verdict_json is None)
    if degenerate is not None:
        return degenerate
    legacy = _parse(legacy_verdict_json)
    candidate = _parse(candidate_verdict_json)
    if legacy is None or candidate is None:
        return _missing("crystallizer_category_match: verdict blob not JSON")
    lv = legacy.get("category") or legacy.get("__value__")
    cv = candidate.get("category") or candidate.get("__value__")
    if lv == cv:
        return ScorerResult(score=1.0)
    return ScorerResult(score=0.0, reason=f"category: legacy={lv!r} != candidate={cv!r}")


# ── LLM judge ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JudgeGrade:
    """Structured grade returned by :class:`LLMJudge`."""

    score: float
    rationale: str


# Callable protocol for a "judge LLM" — input prompt → (score, rationale).
# Typed loosely so tests can pass a lambda and real wiring can pass
# :meth:`ClaudeClient.structured_complete` with a thin adapter.
JudgeCallable = Callable[[str], JudgeGrade]


class LLMJudge:
    """Scores the readiness ``summary`` free-text field via an LLM judge.

    The judge model MUST be a different provider/model family from the
    candidate model (external research §F: if judge and candidate share
    training data, the grades are correlated and the eval is useless).
    We do not enforce this with a runtime check — that would require a
    ``provider`` registry we do not want to own — but callers are
    documented to wire it explicitly. Tests assert on the
    ``candidate_model`` / ``judge_model`` metadata to prove separation.
    """

    __name__ = "llm_judge_readiness_quality"

    def __init__(
        self,
        *,
        judge: JudgeCallable,
        judge_model: str,
        candidate_model: str,
        minimum_passing_score: float = 0.7,
    ) -> None:
        if judge_model == candidate_model:
            raise ValueError(
                "LLMJudge: judge_model must differ from candidate_model to avoid "
                "correlated grading (external research §F)."
            )
        self._judge = judge
        self._judge_model = judge_model
        self._candidate_model = candidate_model
        self._threshold = minimum_passing_score

    def __call__(
        self,
        legacy_verdict_json: str,
        candidate_verdict_json: str | None,
    ) -> ScorerResult:
        degenerate = _degenerate_when(candidate_verdict_json is None)
        if degenerate is not None:
            return degenerate
        legacy = _parse(legacy_verdict_json)
        candidate = _parse(candidate_verdict_json)
        if legacy is None or candidate is None:
            return _missing("llm_judge: verdict blob not JSON")

        legacy_summary = legacy.get("summary")
        candidate_summary = candidate.get("summary")
        if not isinstance(legacy_summary, str) or not isinstance(candidate_summary, str):
            return _missing("llm_judge: summary field missing or not a string")

        prompt = self._build_prompt(legacy_summary, candidate_summary)
        try:
            grade = self._judge(prompt)
        except Exception as exc:  # noqa: BLE001 — judge must fail-open
            logger.warning(
                "llm_judge_failed event=llm_judge_failed judge_model=%s error=%s: %s",
                self._judge_model,
                type(exc).__name__,
                exc,
            )
            return ScorerResult(
                score=0.0,
                reason=f"judge_error: {type(exc).__name__}: {exc}",
                metadata={"judge_model": self._judge_model, "judge_error": True},
            )

        passing = grade.score >= self._threshold
        return ScorerResult(
            score=1.0 if passing else 0.0,
            reason=None if passing else grade.rationale,
            metadata={
                "judge_model": self._judge_model,
                "candidate_model": self._candidate_model,
                "judge_raw_score": grade.score,
                "threshold": self._threshold,
            },
        )

    def _build_prompt(self, legacy_summary: str, candidate_summary: str) -> str:
        # Plain text prompt — the adapter layer is responsible for
        # wrapping it in the judge SDK's specific schema.
        return (
            "You are grading two short summaries of a pull-request merge-readiness "
            "verdict. Score the CANDIDATE summary against the LEGACY summary on a "
            "0.0–1.0 scale where 1.0 means the candidate faithfully captures the "
            "same merge-gating information. Return JSON: "
            '{"score": <float>, "rationale": <one sentence>}.\n\n'
            f"LEGACY:\n{legacy_summary}\n\nCANDIDATE:\n{candidate_summary}\n"
        )


# ── Registry ─────────────────────────────────────────────────────────────


# Explicit, single source of truth for "which scorers run at which
# decision site". The harness iterates over this map; the admin API
# uses it to render the per-site agreement rates in a stable order.

DEFAULT_SCORER_REGISTRY: dict[str, tuple[Scorer, ...]] = {
    "readiness": (readiness_verdict_match,),
    "ci_triage": (ci_triage_category_match,),
    "issue_triage": (issue_triage_kind_match,),
    "dispatch_guard": (dispatch_guard_match,),
    "review_classification": (review_classification_match,),
    "cascade": (cascade_action_match,),
    "stuck_pr": (stuck_pr_match,),
    "bot_identity": (bot_identity_match,),
    "executor_routing": (executor_routing_match,),
    "crystallizer_category": (crystallizer_category_match,),
}

# ``llm_judge_readiness_quality`` is attached separately at harness
# construction time so the caller can inject a judge callable rather
# than the registry trying to auto-wire an LLM client.


__all__ = [
    "DEFAULT_SCORER_REGISTRY",
    "JudgeCallable",
    "JudgeGrade",
    "LLMJudge",
    "Scorer",
    "ScorerResult",
    "bot_identity_match",
    "cascade_action_match",
    "ci_triage_category_match",
    "crystallizer_category_match",
    "dispatch_guard_match",
    "executor_routing_match",
    "issue_triage_kind_match",
    "readiness_verdict_match",
    "review_classification_match",
    "stuck_pr_match",
]
