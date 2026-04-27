"""Output filtering for LLM-authored content about to hit the GitHub API.

Every outbound write that carries LLM-authored text — status comments,
issue bodies, PR descriptions, check-run summaries — runs through
:func:`filter_output` before the HTTP call. The filter catches three
classes of failure that sanitize-on-input cannot:

1. **Injection echo** — the model parrots a prompt-injection sigil from a
   poisoned upstream back into an outbound artefact.
2. **Marker spoofing** — the model emits one of caretaker's reserved
   ``<!-- caretaker:* -->`` HTML-comment markers. Those markers are how
   the dispatch-guard tells self-echoes from human prompts; an LLM that
   can emit them can bypass the guard entirely.
3. **Render-time shenanigans** — zero-width chars, bidi overrides, ANSI
   escape sequences, visible-vs-target URL mismatches. None of these
   belong in a GitHub comment; all are standard social-engineering
   payloads that the LLM can pick up from inputs the sanitizer didn't
   see (memory retrieval hits, diff hunks, etc.).

The filter is **lossy by design**: it rewrites the content in place,
records the reasons on the returned :class:`FilteredOutput`, and never
raises. Call sites that need a hard stop check the ``blocked_reasons``
list and refuse to POST when it is non-empty AND the policy wants a
hard block.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from caretaker.guardrails.policy import OutputPolicy, default_policies
from caretaker.guardrails.sanitize import (
    _load_sigils,  # reuse the sigil list for echo detection
    _strip_invisible,
)
from caretaker.observability.metrics import record_guardrail_filter_blocked

logger = logging.getLogger(__name__)


OutputTarget = Literal[
    "github_comment",
    "github_pr_body",
    "github_issue_body",
    "check_run_output",
]


@dataclass(frozen=True, slots=True)
class FilteredOutput:
    """Return value of :func:`filter_output`.

    ``blocked_reasons`` enumerates every policy hit. The content field is
    the rewritten string (never ``None``); callers should POST that over
    the original.  ``original_size`` / ``filtered_size`` are byte counts
    suitable for dashboard rollups.
    """

    content: str
    blocked_reasons: list[str] = field(default_factory=list)
    original_size: int = 0
    filtered_size: int = 0


# Caretaker reserved marker pattern (mirrors sanitize._CARETAKER_MARKER_RE
# but matched against outbound content, which we are generating).
_CARETAKER_MARKER_RE = re.compile(
    r"<!--\s*caretaker:[a-z0-9:_\-]+(?:\s*-->)?",
    re.IGNORECASE,
)

# ANSI / shell escape sequences. Primarily CSI parameters (``ESC [``) and
# OSC (``ESC ]``); we strip both because rendered GitHub comments let
# them through and terminal readers then see colour + cursor tricks.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

# Hidden-link pattern: a Markdown link where the visible text contains a
# URL that differs from the link target. Examples an LLM might parrot
# from a compromised issue:
#   [https://example.com](https://attacker.example/payload)
# We are deliberately conservative: we only flag when the visible text
# *contains* a full URL. Links like ``[click here](https://real.com)``
# are fine.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_URL_IN_TEXT_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TRUNCATED_MARKER = "\n\n... [truncated by guardrails]"


def _extract_domain(url: str) -> str:
    """Return the ``host`` portion of a URL, best-effort.

    We avoid ``urllib.parse.urlparse`` for performance (this helper runs
    per link on every filtered output) and because we only need a
    case-folded prefix match against the target URL. The regex below is
    good enough for the "do these two URLs point at the same host"
    question the hidden-link check asks.
    """
    m = re.match(r"https?://([^/?#\s]+)", url, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _strip_caretaker_markers_out(text: str) -> tuple[str, int]:
    new_text, count = _CARETAKER_MARKER_RE.subn("", text)
    return new_text, count


def _strip_ansi_escapes(text: str) -> tuple[str, int]:
    if "\x1b" not in text:
        return text, 0
    new_text, count = _ANSI_ESCAPE_RE.subn("", text)
    return new_text, count


def _neutralise_hidden_links(text: str) -> tuple[str, int]:
    """Rewrite deceptive Markdown links. Returns (rewritten, count).

    "Deceptive" means the visible text contains a URL whose host does
    not match the target's host. We keep the link but rewrite it into
    plain text ``visible → target`` so the reader can see both sides.
    Legitimate links (``[click](https://real.com)``) are untouched.
    """
    changed = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal changed
        visible = match.group(1)
        target = match.group(2).strip()
        if not target.lower().startswith(("http://", "https://")):
            return match.group(0)
        visible_urls = _URL_IN_TEXT_RE.findall(visible)
        if not visible_urls:
            return match.group(0)
        target_host = _extract_domain(target)
        if any(_extract_domain(u) != target_host for u in visible_urls):
            changed += 1
            return f"{visible} -> {target}"
        return match.group(0)

    return _MARKDOWN_LINK_RE.sub(_replace, text), changed


def _detect_echo_sigils(text: str) -> list[str]:
    """Return the lowercase sigils found in ``text`` (reuses sigil list)."""
    sigils = _load_sigils()
    if not sigils:
        return []
    lowered = text.lower()
    return [s for s in sigils if s in lowered]


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    # Reserve room for the truncation marker.
    headroom = max(cap - len(_TRUNCATED_MARKER), 0)
    return text[:headroom] + _TRUNCATED_MARKER, True


def filter_output(
    target: OutputTarget,
    content: str,
    *,
    policy: OutputPolicy | None = None,
) -> FilteredOutput:
    """Apply the output guardrails before an LLM-authored string is written.

    Parameters
    ----------
    target
        Bounded enum of destinations. Drives the default policy when
        ``policy`` is ``None``.
    content
        The LLM-authored content. ``None``-equivalents (empty string)
        return an empty :class:`FilteredOutput`.
    policy
        Optional override for the target's default policy (see
        :func:`caretaker.guardrails.policy.default_policies`).

    The filter is idempotent: passing a previously-filtered string
    through again yields the same content and an empty
    ``blocked_reasons`` (nothing more to strip).
    """
    original_size = len(content.encode("utf-8")) if content else 0
    if not content:
        return FilteredOutput(
            content="",
            blocked_reasons=[],
            original_size=0,
            filtered_size=0,
        )

    pol = policy or default_policies().get(target) or OutputPolicy()
    reasons: list[str] = []
    cleaned = content

    # Step 1: strip caretaker markers. Non-negotiable; this is the
    # dispatch-guard backdoor closing move.
    if pol.block_caretaker_markers:
        cleaned, marker_count = _strip_caretaker_markers_out(cleaned)
        if marker_count:
            reasons.append("caretaker_marker")

    # Step 2: strip ANSI/shell escape sequences.
    if pol.block_shell_escapes:
        cleaned, ansi_count = _strip_ansi_escapes(cleaned)
        if ansi_count:
            reasons.append("shell_escape")

    # Step 3: strip zero-width + bidi-override characters (reused from
    # sanitize so the two sides use identical rules).
    if pol.block_hidden_links:
        cleaned, invisible_count = _strip_invisible(cleaned)
        if invisible_count:
            reasons.append("zero_width")
        cleaned, hidden_count = _neutralise_hidden_links(cleaned)
        if hidden_count:
            reasons.append("hidden_link")

    # Step 4: detect injection-sigil echoes. We do NOT strip the sigil
    # from the outbound body by default — if the model produced one, it
    # almost certainly wrapped other content around it and silent
    # deletion would mangle the prose. Instead we record the reason so
    # strict-mode callers can refuse the write.
    if pol.echo_sigils:
        sigils = _detect_echo_sigils(cleaned)
        if sigils:
            reasons.append("sigil_echo")

    # Step 5: length cap.
    cleaned, was_truncated = _truncate(cleaned, pol.max_length)
    if was_truncated:
        reasons.append("length_cap")

    filtered_size = len(cleaned.encode("utf-8"))

    # One metric increment per unique reason so a noisy output with 20
    # ANSI sequences doesn't inflate the counter 20x.
    for reason in set(reasons):
        record_guardrail_filter_blocked(target=target, reason=reason)

    return FilteredOutput(
        content=cleaned,
        blocked_reasons=reasons,
        original_size=original_size,
        filtered_size=filtered_size,
    )


__all__ = [
    "FilteredOutput",
    "OutputTarget",
    "filter_output",
]
