"""Routing decider — score-based selection between inline LLM and a hand-off reviewer.

Score breakdown (0-100):
  - LOC bucket:        0-30 pts  (additions + deletions)
  - File count:        0-20 pts
  - Sensitive files:   0-25 pts  (workflows, secrets, infra, auth, migrations)
  - Architecture:      0-15 pts  (config changes, many packages touched)
  - Label signals:     0-10 pts  (operator-applied labels)

Score >= threshold (default 40) → hand-off backend (complex review).
Score <  threshold              → inline LLM review  (fast path).

The hand-off backend (``claude_code``, ``opencode``, …) is picked by the
caller from :class:`PRReviewerConfig.complex_reviewer`; this module
returns the ``inline``/``handoff`` decision and the chosen backend name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# File patterns that add to the sensitivity score.
_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\.github/workflows/", re.I), 15),
    (re.compile(r"\.github/", re.I), 8),
    (re.compile(r"(secret|credential|auth|token|password|key)", re.I), 10),
    (re.compile(r"(migration|schema|alembic)", re.I), 8),
    (re.compile(r"(infra|terraform|k8s|helm|deploy)", re.I), 8),
    (re.compile(r"(security|audit)", re.I), 8),
    (re.compile(r"pyproject\.toml|setup\.py|Cargo\.toml|go\.mod|package\.json", re.I), 5),
]

# PR labels that signal complexity.
_COMPLEX_LABELS: frozenset[str] = frozenset(
    {"architecture", "needs-prd", "breaking-change", "refactor", "migration"}
)
# PR labels that signal simplicity.
_SIMPLE_LABELS: frozenset[str] = frozenset({"good-first-issue", "chore", "docs", "typo"})


@dataclass(frozen=True)
class RoutingDecision:
    score: int
    use_inline: bool
    reason: str
    # Hand-off backend to dispatch to when ``use_inline`` is False.
    # Empty string means "the caller decides" — preserved for the inline
    # path (where backend is irrelevant) and for backwards compatibility
    # with existing test fixtures that don't care about the field.
    backend: str = ""


def decide(
    *,
    additions: int,
    deletions: int,
    file_count: int,
    file_paths: list[str],
    pr_labels: list[str],
    threshold: int = 40,
    backend: str = "claude_code",
) -> RoutingDecision:
    """Return a routing decision for a PR."""
    score = 0
    reasons: list[str] = []

    # LOC (0-30)
    loc = additions + deletions
    if loc > 800:
        loc_pts = 30
    elif loc > 400:
        loc_pts = 20
    elif loc > 150:
        loc_pts = 12
    elif loc > 50:
        loc_pts = 6
    else:
        loc_pts = 0
    score += loc_pts
    if loc_pts:
        reasons.append(f"loc={loc}(+{loc_pts})")

    # File count (0-20)
    if file_count > 20:
        fc_pts = 20
    elif file_count > 10:
        fc_pts = 12
    elif file_count > 5:
        fc_pts = 6
    else:
        fc_pts = 0
    score += fc_pts
    if fc_pts:
        reasons.append(f"files={file_count}(+{fc_pts})")

    # Sensitive file patterns (capped at 25)
    sensitive_pts = 0
    matched: set[str] = set()
    for path in file_paths:
        for pattern, pts in _SENSITIVE_PATTERNS:
            key = pattern.pattern
            if key not in matched and pattern.search(path):
                sensitive_pts = min(25, sensitive_pts + pts)
                matched.add(key)
    score += sensitive_pts
    if sensitive_pts:
        reasons.append(f"sensitive_files(+{sensitive_pts})")

    # Architecture signals — many different packages/dirs (0-15)
    top_dirs: set[str] = set()
    for p in file_paths:
        top_dirs.add(p.split("/")[0] if "/" in p else "")
    if len(top_dirs) > 6:
        arch_pts = 15
    elif len(top_dirs) > 3:
        arch_pts = 8
    else:
        arch_pts = 0
    score += arch_pts
    if arch_pts:
        reasons.append(f"dirs={len(top_dirs)}(+{arch_pts})")

    # Label signals (0-10)
    label_set = {lbl.lower() for lbl in pr_labels}
    if label_set & _COMPLEX_LABELS:
        score += 10
        reasons.append("complex_label(+10)")
    elif label_set & _SIMPLE_LABELS:
        score = max(0, score - 10)
        reasons.append("simple_label(-10)")

    score = min(100, max(0, score))
    use_inline = score < threshold
    path_str = ", ".join(reasons) if reasons else "low-complexity"
    chosen = "inline" if use_inline else backend
    return RoutingDecision(
        score=score,
        use_inline=use_inline,
        reason=f"score={score} [{path_str}] → {chosen}",
        backend="" if use_inline else backend,
    )
