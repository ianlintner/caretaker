"""Input sanitization for external content feeding LLM prompts.

Every external-input boundary in caretaker (issue bodies, PR comments,
review bodies, CI logs, dependabot PR bodies, webhook payload bits)
passes through :func:`sanitize_input` *before* being interpolated into
an LLM prompt. The sanitizer is deliberately narrow: it does **not**
try to classify semantics or censor "bad words"; it removes the classes
of byte patterns that turn benign-looking text into a prompt-injection
payload or a render-time exploit.

What it does
------------

* Normalises to Unicode NFKC and strips non-printable code points
  (zero-width space, zero-width joiner, RLO/LRO direction overrides,
  bidirectional embedding markers — the Trojan-Source family).
* Strips known prompt-injection sigils from a shipped text file at
  :data:`DEFAULT_SIGIL_LIST_PATH`. Operators can point at their own list
  via ``guardrails.sigil_list_path`` without a release.
* Truncates to a per-source byte budget, taking the **tail** for log-like
  sources (CI logs put the failing assertion at the end) and the **head**
  for everything else (bodies usually front-load the signal).
* Records every modification on the returned :class:`SanitizedInput`
  so audit paths have a full accounting of what was changed and why.

What it does NOT do
-------------------

* It does not validate structure (that is the job of pydantic schemas
  downstream).
* It does not re-escape Markdown or HTML (the destination prompt fencing
  is the right layer for that — sanitizer output is still plain text).
* It does not redact secrets (secret scanning runs upstream at the
  webhook boundary; the sigil list is specifically about *injection*).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from caretaker.observability.metrics import record_guardrail_sanitize

if TYPE_CHECKING:
    import os

logger = logging.getLogger(__name__)


# ── Source enum ──────────────────────────────────────────────────────────

InputSource = Literal[
    "github_issue_body",
    "github_comment",
    "github_review_body",
    "ci_log",
    "dependabot_body",
    "webhook_comment_body",
    "pr_body",
    "other",
]


# Per-source byte budgets. Keeping these as a module-level dict rather
# than a dataclass so operators reading this file can see the defaults
# in one line. Sizes err on the small side; hot paths that need more
# context can pass ``max_bytes`` through to :func:`sanitize_input`.
_DEFAULT_BUDGETS: dict[InputSource, int] = {
    "github_issue_body": 8192,
    "github_comment": 4096,
    "github_review_body": 4096,
    "ci_log": 32768,
    "dependabot_body": 16384,
    "webhook_comment_body": 4096,
    "pr_body": 16384,
    "other": 4096,
}

# Sources whose failure signal is at the tail. For logs the interesting
# assertion is the last page; for bodies it is usually at the top.
_TAIL_SOURCES: frozenset[InputSource] = frozenset({"ci_log"})


# ── Modification record ──────────────────────────────────────────────────


class ModificationType(StrEnum):
    """Closed enum of sanitizer modification kinds.

    Used as a metric label and as an audit breadcrumb on
    :class:`SanitizedInput`. Values are kept short and grep-able.
    """

    SIGIL_STRIPPED = "sigil_stripped"
    ZERO_WIDTH_STRIPPED = "zero_width_stripped"
    NON_PRINTABLE_STRIPPED = "non_printable_stripped"
    NFKC_NORMALISED = "nfkc_normalised"
    TRUNCATED_HEAD = "truncated_head"
    TRUNCATED_TAIL = "truncated_tail"
    CARETAKER_MARKER_STRIPPED = "caretaker_marker_stripped"


@dataclass(frozen=True, slots=True)
class Modification:
    """One audit entry describing a single sanitizer change."""

    type: ModificationType
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SanitizedInput:
    """Return value of :func:`sanitize_input`.

    Callers should always prefer ``result.content`` over the raw input
    string. ``modifications`` is the audit trail; ``original_size`` /
    ``sanitized_size`` are there so operators can eyeball "how much did
    we cut" without diffing two blobs.
    """

    content: str
    modifications: list[Modification] = field(default_factory=list)
    original_size: int = 0
    sanitized_size: int = 0


# ── Sigil loader ─────────────────────────────────────────────────────────


DEFAULT_SIGIL_LIST_PATH: Path = Path(__file__).parent / "sigils.txt"

# Cache the loaded sigils so every call site does not re-read the file.
# ``_sigil_cache`` keys on the absolute path so operator overrides via a
# distinct path don't collide with the default list.
_sigil_cache: dict[str, list[str]] = {}


def _load_sigils(path: Path | None = None) -> list[str]:
    """Return the lowercase sigil patterns from ``path`` (or the default).

    The file format is one substring per line. Lines starting with ``#``
    are comments; blank lines are skipped. Matching is case-insensitive
    substring against NFKC-normalised + lowercased text.
    """
    resolved = (path or DEFAULT_SIGIL_LIST_PATH).resolve()
    cache_key = str(resolved)
    cached = _sigil_cache.get(cache_key)
    if cached is not None:
        return cached
    patterns: list[str] = []
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("guardrails: could not read sigil list at %s: %s", resolved, exc)
        _sigil_cache[cache_key] = []
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line.lower())
    _sigil_cache[cache_key] = patterns
    return patterns


def reset_sigil_cache() -> None:
    """Test helper — drop the cached sigil list so reloads pick up edits."""
    _sigil_cache.clear()


# ── Internal helpers ─────────────────────────────────────────────────────

# Zero-width + Trojan-Source Unicode points. Stripping these is the only
# reasonable default — they have no legitimate use in code-review prose
# and are the go-to payload for homoglyph + bidi attacks.
_INVISIBLE_CHARS: frozenset[str] = frozenset(
    [
        "​",  # ZERO WIDTH SPACE
        "‌",  # ZERO WIDTH NON-JOINER
        "‍",  # ZERO WIDTH JOINER
        "‎",  # LEFT-TO-RIGHT MARK
        "‏",  # RIGHT-TO-LEFT MARK
        "‪",  # LRE
        "‫",  # RLE
        "‬",  # PDF
        "‭",  # LRO
        "‮",  # RLO
        "⁦",  # LRI
        "⁧",  # RLI
        "⁨",  # FSI
        "⁩",  # PDI
        "﻿",  # BYTE ORDER MARK
    ]
)


# HTML-comment caretaker markers ``<!-- caretaker:… -->`` are reserved for
# caretaker's internal dispatch-guard state tracking. When they appear in
# *inbound* content it is almost always because a user pasted a caretaker
# comment back into an issue body; stripping them prevents a dispatch-
# guard bypass via echo. Matching is permissive (missing closing ``-->``
# still strips the marker token) so we also catch truncated payloads.
_CARETAKER_MARKER_RE = re.compile(
    r"<!--\s*caretaker:[a-z0-9:_\-]+(?:\s*-->)?",
    re.IGNORECASE,
)


def _strip_invisible(text: str) -> tuple[str, int]:
    """Remove zero-width + bidi-override characters. Returns (cleaned, count)."""
    if not any(ch in _INVISIBLE_CHARS for ch in text):
        return text, 0
    buf: list[str] = []
    removed = 0
    for ch in text:
        if ch in _INVISIBLE_CHARS:
            removed += 1
            continue
        buf.append(ch)
    return "".join(buf), removed


def _strip_non_printable(text: str) -> tuple[str, int]:
    """Remove non-printable control chars except common whitespace.

    ``\\t`` ``\\n`` ``\\r`` are preserved so normal prose keeps its line
    breaks; everything else in the C0/C1 control ranges is dropped.
    """
    removed = 0
    buf: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch not in ("\t", "\n", "\r"):
            removed += 1
            continue
        buf.append(ch)
    if removed == 0:
        return text, 0
    return "".join(buf), removed


def _strip_sigils(text: str, sigils: list[str]) -> tuple[str, list[str]]:
    """Remove every sigil substring (case-insensitive). Returns (cleaned, hits).

    The ``hits`` list carries the original (lowercase) sigil form of each
    match so audit trails can tell "which pattern tripped". Matching is
    case-insensitive but the returned text preserves the case of anything
    we kept (we only surgically remove the matched substrings).
    """
    if not text or not sigils:
        return text, []
    hits: list[str] = []
    lowered = text.lower()
    # Delete from longest match to shortest so nested sigils don't leave
    # dangling fragments. Using a single-pass index walk keeps the output
    # O(len(text) * len(sigils)) which is fine for the per-source byte
    # budgets we run with.
    for sigil in sorted(sigils, key=len, reverse=True):
        while True:
            idx = lowered.find(sigil)
            if idx == -1:
                break
            hits.append(sigil)
            text = text[:idx] + text[idx + len(sigil) :]
            lowered = lowered[:idx] + lowered[idx + len(sigil) :]
    return text, hits


def _strip_caretaker_markers(text: str) -> tuple[str, int]:
    """Remove ``<!-- caretaker:… -->`` markers. Returns (cleaned, count)."""
    if "caretaker:" not in text.lower():
        return text, 0
    new_text, count = _CARETAKER_MARKER_RE.subn("", text)
    return new_text, count


# ── Public API ───────────────────────────────────────────────────────────


def sanitize_input(
    source: InputSource,
    content: str,
    *,
    max_bytes: int | None = None,
    sigil_list_path: str | os.PathLike[str] | None = None,
) -> SanitizedInput:
    """Scrub ``content`` for prompt-injection safety before LLM consumption.

    Parameters
    ----------
    source
        The logical origin of the content. Drives the per-source byte
        budget and the tail-vs-head truncation policy. Use ``"other"``
        for call sites that don't fit the shipped taxonomy; this still
        gets sanitised but with conservative defaults.
    content
        The raw external string. ``None``-equivalents (``""``) are
        allowed; the function returns an empty :class:`SanitizedInput`.
    max_bytes
        Per-call override for the source's default budget. Rarely
        needed — prefer adjusting :data:`_DEFAULT_BUDGETS` or, for
        call-site-specific overrides, passing this keyword.
    sigil_list_path
        Optional override for the sigil file to load. Maps to
        :attr:`GuardrailsConfig.sigil_list_path`. ``None`` means the
        shipped default list.

    Returns
    -------
    SanitizedInput
        Carries the cleaned content and a list of :class:`Modification`
        records describing every change, plus the before/after byte
        counts for dashboard rollups.
    """
    original_size = len(content.encode("utf-8")) if content else 0
    if not content:
        return SanitizedInput(
            content="",
            modifications=[],
            original_size=0,
            sanitized_size=0,
        )

    modifications: list[Modification] = []

    # Step 1: Unicode NFKC normalisation. NFKC collapses compatibility
    # variants (fullwidth ASCII, stylistic ligatures) into a single
    # canonical form so sigil matching is robust against the standard
    # homoglyph workaround. We only record the modification if the
    # normalisation actually changed the string.
    normalised = unicodedata.normalize("NFKC", content)
    if normalised != content:
        modifications.append(Modification(ModificationType.NFKC_NORMALISED))
    cleaned = normalised

    # Step 2: strip invisible / bidi-override characters.
    cleaned, invisible_count = _strip_invisible(cleaned)
    if invisible_count > 0:
        modifications.append(
            Modification(
                ModificationType.ZERO_WIDTH_STRIPPED,
                detail=f"removed={invisible_count}",
            )
        )

    # Step 3: strip non-printable control characters (keeping \t \n \r).
    cleaned, non_printable_count = _strip_non_printable(cleaned)
    if non_printable_count > 0:
        modifications.append(
            Modification(
                ModificationType.NON_PRINTABLE_STRIPPED,
                detail=f"removed={non_printable_count}",
            )
        )

    # Step 4: strip caretaker markers that snuck into inbound content.
    cleaned, marker_count = _strip_caretaker_markers(cleaned)
    if marker_count > 0:
        modifications.append(
            Modification(
                ModificationType.CARETAKER_MARKER_STRIPPED,
                detail=f"removed={marker_count}",
            )
        )

    # Step 5: strip prompt-injection sigils.
    sigils = _load_sigils(Path(sigil_list_path) if sigil_list_path else None)
    cleaned, sigil_hits = _strip_sigils(cleaned, sigils)
    for hit in sigil_hits:
        modifications.append(
            Modification(
                ModificationType.SIGIL_STRIPPED,
                detail=hit,
            )
        )

    # Step 6: truncate to per-source byte budget. Budget is bytes so we
    # operate on the UTF-8-encoded representation; on truncation we slice
    # back to a valid string boundary so we never emit a lone continuation
    # byte. Logs get the tail; bodies get the head.
    budget = max_bytes if max_bytes is not None else _DEFAULT_BUDGETS.get(source, 4096)
    encoded = cleaned.encode("utf-8")
    if len(encoded) > budget:
        tail_policy = source in _TAIL_SOURCES
        if tail_policy:
            sliced = encoded[-budget:]
            mod_type = ModificationType.TRUNCATED_TAIL
        else:
            sliced = encoded[:budget]
            mod_type = ModificationType.TRUNCATED_HEAD
        # Decode safely — drop a partial trailing (or leading) codepoint
        # rather than raising ``UnicodeDecodeError``.
        cleaned = sliced.decode("utf-8", errors="ignore")
        modifications.append(
            Modification(
                mod_type,
                detail=f"from={len(encoded)}B to={len(cleaned.encode('utf-8'))}B",
            )
        )

    sanitized_size = len(cleaned.encode("utf-8"))

    # Emit metrics once per unique modification type for this call. Using
    # a set avoids firing 50 counters for a log tail with 50 sigil hits.
    for mod_type_seen in {m.type.value for m in modifications}:
        record_guardrail_sanitize(source=source, modification_type=mod_type_seen)

    return SanitizedInput(
        content=cleaned,
        modifications=modifications,
        original_size=original_size,
        sanitized_size=sanitized_size,
    )


__all__ = [
    "DEFAULT_SIGIL_LIST_PATH",
    "InputSource",
    "Modification",
    "ModificationType",
    "SanitizedInput",
    "reset_sigil_cache",
    "sanitize_input",
]
