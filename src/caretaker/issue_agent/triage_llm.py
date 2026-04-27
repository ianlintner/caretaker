"""LLM-backed issue triage candidate for the Phase 2 agentic migration.

This module implements the §3.5 "Issue triage + duplicate detection"
handover: one :func:`~caretaker.llm.claude.ClaudeClient.structured_complete`
call replaces the keyword ladder in
:func:`caretaker.issue_agent.classifier.classify_issue` plus the title-hash
grouping in :mod:`caretaker.issue_agent.issue_triage` (for the in-process
classification path only — the bulk triage pass keeps the deterministic
hash grouping since it is a batch operation over hundreds of issues).

Surface:

* :class:`IssueTriage` — pydantic schema the LLM fills in.
* :class:`IssueCandidate` — compact record of a nearby issue the LLM
  may cite as a duplicate source.
* :func:`classify_issue_llm` — the candidate side of the shadow pair.
* :func:`legacy_to_triage` — adapter that wraps the classic
  :class:`~caretaker.issue_agent.classifier.IssueClassification` verdict
  as an :class:`IssueTriage`, so the shadow decorator can compare the two
  on a single type without every call site knowing both vocabularies.
* :func:`select_candidates_by_jaccard` — keyword-overlap candidate
  pre-selection. The repo has no embedding provider configured today, so
  the fall-back path is the primary path; if an embedding provider lands
  later it can live behind the same signature.

The CVE regex pre-filter is preserved on purpose: structured CVE
identifiers are the one thing the legacy regex grouping gets right, so
when a title or body mentions a CVE we feed it as a typed hint to the
LLM prompt ("issues that share this CVE are duplicate candidates") and
still let the LLM weigh severity + duplicate-of independently.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from caretaker.guardrails import sanitize_input
from caretaker.issue_agent.classifier import IssueClassification
from caretaker.llm.claude import StructuredCompleteError

if TYPE_CHECKING:
    from caretaker.github_client.models import Issue
    from caretaker.llm.claude import ClaudeClient

logger = logging.getLogger(__name__)


_CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
# Body trim — 2k chars is enough for the LLM to judge severity and still
# leaves room for candidate context in the 8k prompt window the shared
# Claude config budgets for structured_complete calls.
_BODY_TRUNCATE = 2000
# Keyword-overlap tokeniser. Strips markdown fences and punctuation, keeps
# words of length ≥3 so "bug"/"crash" survive but "is"/"to" do not.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "have",
        "has",
        "was",
        "were",
        "are",
        "you",
        "your",
        "but",
        "not",
        "out",
        "off",
        "its",
        "also",
        "when",
        "while",
        "then",
        "than",
        "issue",
        "issues",
        "bug",
        "error",
        "problem",
    }
)


IssueKind = Literal["bug", "feature", "question", "docs", "chore", "security", "other"]
IssueSeverity = Literal["blocker", "major", "minor", "nit"]
IssueStaleness = Literal["fresh", "stale", "ancient"]


class IssueCandidate(BaseModel):
    """Compact representation of a nearby issue the LLM may cite as a dup source.

    Kept deliberately small (number + title + labels) so a batch of 5 fits
    well under the prompt budget; the caller is responsible for picking
    which candidates are worth showing (see :func:`select_candidates_by_jaccard`).
    """

    number: int
    title: str
    labels: list[str] = Field(default_factory=list)


class IssueTriage(BaseModel):
    """Structured triage verdict for a single issue.

    This is the schema the LLM fills in and the common type the shadow
    decorator compares over — :func:`legacy_to_triage` wraps the classic
    heuristic verdict into the same shape so both sides speak the same
    vocabulary.
    """

    kind: IssueKind
    severity: IssueSeverity | None = None  # bugs only; None for non-bug kinds
    suggested_labels: list[str] = Field(default_factory=list)
    duplicate_of: int | None = None  # issue number if the LLM identifies a dup
    duplicate_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    staleness: IssueStaleness = "fresh"
    summary_one_line: str = Field(max_length=150)


# ── Candidate selection ─────────────────────────────────────────────────


def _tokenise(text: str) -> set[str]:
    """Return the set of normalised tokens from ``text`` for Jaccard."""
    tokens = {tok.lower() for tok in _TOKEN_RE.findall(text)}
    return tokens - _STOPWORDS


def select_candidates_by_jaccard(
    issue: Issue,
    pool: list[Issue],
    *,
    limit: int = 5,
) -> list[IssueCandidate]:
    """Pick up to ``limit`` nearby issues from ``pool`` by title+body Jaccard.

    The repo currently has no embedding provider wired in; this is the
    fallback path called out in the migration plan. If one lands later,
    a sibling ``select_candidates_by_embedding`` can slot in behind the
    same :class:`IssueCandidate` surface.

    Self-match is filtered out so an issue never gets cited as its own
    duplicate.
    """
    if limit <= 0 or not pool:
        return []

    target_tokens = _tokenise(f"{issue.title}\n{issue.body or ''}")
    if not target_tokens:
        return []

    scored: list[tuple[float, Issue]] = []
    for other in pool:
        if other.number == issue.number:
            continue
        other_tokens = _tokenise(f"{other.title}\n{other.body or ''}")
        if not other_tokens:
            continue
        intersection = target_tokens & other_tokens
        if not intersection:
            continue
        union = target_tokens | other_tokens
        score = len(intersection) / len(union)
        scored.append((score, other))

    # Highest similarity first; stable on ties via issue number ascending.
    scored.sort(key=lambda pair: (-pair[0], pair[1].number))
    return [
        IssueCandidate(
            number=other.number,
            title=other.title,
            labels=[lbl.name for lbl in other.labels],
        )
        for _score, other in scored[:limit]
    ]


# ── Prompt construction ─────────────────────────────────────────────────


_PROMPT_TEMPLATE = """\
You are triaging a GitHub issue for the caretaker automation bot.

Classify the issue into one of: bug, feature, question, docs, chore, security, other.
For bugs, also pick severity: blocker, major, minor, or nit. For non-bugs, leave severity null.

Also judge:
- suggested_labels: up to 4 labels that would apply (free-form strings).
- duplicate_of: if one of the candidate issues below clearly covers the same problem,
  set this to its number and set duplicate_confidence in [0, 1]. Otherwise leave both null.
- staleness: "fresh", "stale", or "ancient" — use the age_days field below to decide.
- summary_one_line: ≤150 chars, plain English, no markdown.

Return exactly one JSON object matching the schema provided in the system prompt.

Issue:
  title: {title}
  age_days: {age_days}
  labels: {labels}
  assignees: {assignees}
{cve_hint}
  body:
{body}

Duplicate candidates (nearby open issues; pick at most one as duplicate_of, or none):
{candidates_block}
"""


def _render_candidates(candidates: list[IssueCandidate]) -> str:
    if not candidates:
        return "  (no nearby candidates)"
    lines: list[str] = []
    for cand in candidates:
        label_str = ",".join(cand.labels) if cand.labels else "-"
        # Truncate title so one misbehaving title can't dominate the prompt.
        title = cand.title if len(cand.title) <= 120 else cand.title[:117] + "..."
        lines.append(f"  #{cand.number} [{label_str}] {title}")
    return "\n".join(lines)


def _age_days(issue: Issue) -> int:
    ts = issue.updated_at or issue.created_at
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - ts).days)


def _extract_cve(issue: Issue) -> str | None:
    """Return an upper-cased CVE id if the title or body mentions one.

    The CVE regex pre-filter is preserved from the legacy duplicate-grouping
    path; structured CVE identifiers are the one thing regex gets right and
    the LLM benefits from being told that issues sharing a CVE are likely
    duplicates.
    """
    haystack = f"{issue.title}\n{issue.body or ''}"
    match = _CVE_RE.search(haystack)
    return match.group(0).upper() if match else None


def build_prompt(issue: Issue, candidates: list[IssueCandidate]) -> str:
    """Render the user-turn prompt for :func:`classify_issue_llm`.

    Exposed for testing and for reviewers who want to eyeball what we
    actually send to the model. The issue body is run through
    :func:`caretaker.guardrails.sanitize_input` before interpolation so
    prompt-injection sigils pasted into a GitHub issue can never cross
    the LLM boundary (Agentic Design Patterns Ch. 18 Input Validation).
    """
    body_raw = (issue.body or "").strip()
    body = sanitize_input("github_issue_body", body_raw).content
    if len(body) > _BODY_TRUNCATE:
        body = body[:_BODY_TRUNCATE] + "\n[... body truncated]"
    body_indented = "    " + body.replace("\n", "\n    ") if body else "    (empty)"

    cve = _extract_cve(issue)
    cve_hint = (
        f"  cve_hint: {cve} — issues that share this CVE are duplicate candidates.\n" if cve else ""
    )

    return _PROMPT_TEMPLATE.format(
        title=issue.title,
        age_days=_age_days(issue),
        labels=",".join(lbl.name for lbl in issue.labels) or "-",
        assignees=",".join(a.login for a in issue.assignees) or "-",
        cve_hint=cve_hint,
        body=body_indented,
        candidates_block=_render_candidates(candidates),
    )


# ── Candidate function ──────────────────────────────────────────────────


async def classify_issue_llm(
    issue: Issue,
    *,
    candidates: list[IssueCandidate],
    claude: ClaudeClient,
) -> IssueTriage | None:
    """Ask the LLM to triage ``issue``; return ``None`` on any failure.

    The signature matches the Phase 2 candidate-function convention: all
    inputs are keyword-only except the issue itself, and the return type
    is ``IssueTriage | None`` so the ``@shadow_decision`` wrapper can treat
    a ``None`` as a candidate failure and fall through to legacy.

    Errors raised by :func:`ClaudeClient.structured_complete` are logged and
    converted to ``None`` — the shadow decorator handles the rest.
    """
    prompt = build_prompt(issue, candidates)
    try:
        verdict = await claude.structured_complete(
            prompt,
            schema=IssueTriage,
            feature="issue_triage",
        )
    except StructuredCompleteError as exc:
        logger.warning(
            "classify_issue_llm: structured_complete failed for issue #%d: %s",
            issue.number,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — candidate must never raise up
        logger.warning(
            "classify_issue_llm: unexpected error for issue #%d: %s",
            issue.number,
            exc,
        )
        return None

    # If the LLM claimed a duplicate_of that is not in the candidate set,
    # drop it — models will occasionally hallucinate numbers. Keeping the
    # kind/severity/summary fields still adds value.
    if verdict.duplicate_of is not None:
        valid_numbers = {c.number for c in candidates}
        if verdict.duplicate_of not in valid_numbers:
            logger.info(
                "classify_issue_llm: dropping hallucinated duplicate_of=%d "
                "for issue #%d (not in candidate set)",
                verdict.duplicate_of,
                issue.number,
            )
            verdict = verdict.model_copy(
                update={"duplicate_of": None, "duplicate_confidence": None}
            )
    return verdict


# ── Legacy adapter ──────────────────────────────────────────────────────


# Mapping from the legacy StrEnum vocabulary to the new structured kind/
# severity pair. Kept explicit (rather than derived) so future legacy
# additions fail noisily in review instead of silently mapping to ``other``.
_LEGACY_KIND_MAP: dict[IssueClassification, tuple[IssueKind, IssueSeverity | None]] = {
    IssueClassification.BUG_SIMPLE: ("bug", "minor"),
    IssueClassification.BUG_COMPLEX: ("bug", "major"),
    IssueClassification.FEATURE_SMALL: ("feature", None),
    IssueClassification.FEATURE_LARGE: ("feature", None),
    IssueClassification.QUESTION: ("question", None),
    IssueClassification.DUPLICATE: ("other", None),
    IssueClassification.STALE: ("other", None),
    IssueClassification.INFRA_OR_CONFIG: ("chore", None),
    IssueClassification.MAINTAINER_INTERNAL: ("chore", None),
}


def legacy_to_triage(classification: IssueClassification, issue: Issue) -> IssueTriage:
    """Wrap a legacy :class:`IssueClassification` verdict as an :class:`IssueTriage`.

    Keeps ``duplicate_of`` as ``None`` (the legacy hash grouping is run
    separately in :mod:`issue_triage` and not per-issue), and ``staleness``
    as ``"fresh"`` unless the classifier said STALE. The resulting triage
    record is what the shadow decorator actually compares against so both
    sides speak one vocabulary.
    """
    kind, severity = _LEGACY_KIND_MAP.get(classification, ("other", None))
    staleness: IssueStaleness = "stale" if classification == IssueClassification.STALE else "fresh"
    # Legacy summary is the issue title — capped at 150 chars to satisfy
    # the schema without a silent truncate at call time.
    raw_summary = issue.title.strip() or f"Issue #{issue.number}"
    summary = raw_summary if len(raw_summary) <= 150 else raw_summary[:147] + "..."
    return IssueTriage(
        kind=kind,
        severity=severity,
        suggested_labels=[],
        duplicate_of=None,
        duplicate_confidence=None,
        staleness=staleness,
        summary_one_line=summary,
    )


def compare_triage(a: IssueTriage, b: IssueTriage) -> bool:
    """Shadow-mode comparator: agree iff ``(kind, duplicate_of)`` match.

    Suggested labels, severity, and one-line summaries are noisy by design
    (the LLM rewords things, the legacy adapter can't guess severity), so
    counting every micro-delta as a disagreement would drown the real
    signal. ``(kind, duplicate_of)`` captures the two decisions operators
    actually need to audit before flipping authority.
    """
    return (a.kind, a.duplicate_of) == (b.kind, b.duplicate_of)


__all__ = [
    "IssueCandidate",
    "IssueKind",
    "IssueSeverity",
    "IssueStaleness",
    "IssueTriage",
    "build_prompt",
    "classify_issue_llm",
    "compare_triage",
    "legacy_to_triage",
    "select_candidates_by_jaccard",
]
