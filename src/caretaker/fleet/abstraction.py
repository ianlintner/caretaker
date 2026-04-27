"""Abstraction pass for fleet-tier :GlobalSkill promotion.

Plan reference: ``docs/memory-graph-plan.md`` §5.2 / §5.4. The
:class:`~caretaker.graph.models.NodeType.GLOBAL_SKILL` tier is a
cross-tenant shared surface — a per-repo :Skill may be promoted to
:GlobalSkill only when the same signature has been observed in at
least ``fleet.min_repos_for_promotion`` distinct repos **and** the
SOP text has been run through this redactor.

The redactor is intentionally a best-effort identifier stripper, not
a proof of privacy. It targets the four identifier classes that show
up in actual caretaker SOPs (repo slugs, GitHub handles, PR/issue
refs, and repo-embedded file paths) and leaves everything else alone.
Callers MUST treat the output as still semi-sensitive: the promotion
workflow pairs this pass with a per-skill human opt-out flag before
anything actually crosses a tenant boundary.

Design notes
------------

* Pure function. No I/O, no global state. Deterministic on identical
  input + deny_list — safe to call from tests and from background
  promotion batches alike.
* Idempotent: running the output back through :func:`abstract_sop`
  returns the same string. The placeholders (``<repo>``, ``<user>``,
  ``<ref>``, ``<path>``) are deliberately chosen to not overlap with
  any of the source patterns so a second pass is a no-op.
* Order matters. File paths are scrubbed first — a path like
  ``src/acme/widget/main.py`` should become ``<path>`` rather than
  leak ``acme`` through the repo-slug stripper. Repo slugs are next,
  then user handles, then PR/issue refs.
"""

from __future__ import annotations

import re

# ``owner/name`` — the canonical GitHub repo slug shape. Same regex used
# by the causal-chain marker parser so behaviour stays consistent
# across caretaker.
_REPO_SLUG = re.compile(r"\b[\w-]+/[\w-]+\b")

# ``@handle`` — GitHub login mentions. ``\w`` is inclusive of digits +
# underscores which matches the GitHub username character set well
# enough for a redactor.
_USER_HANDLE = re.compile(r"@\w+")

# ``#123`` — PR / issue reference. Anchored by ``\b`` so that e.g.
# ``#fragment`` inside a URL is left alone.
_REF = re.compile(r"#\d+\b")


def _path_pattern_for(repo_slug_token: str) -> re.Pattern[str]:
    """Return a regex that strips any path segment containing ``token``.

    We match an optional leading path component, the token, and an
    optional trailing path component so both ``src/acme/widget.py`` and
    ``acme/widget.py`` collapse to ``<path>``. The regex refuses to
    cross whitespace so adjacent prose isn't swallowed.
    """
    # Escape the token so deny_list entries like "acme-widget" (with a
    # dash) still produce a valid regex.
    token = re.escape(repo_slug_token)
    return re.compile(rf"(?:\S+/)?{token}(?:/\S+)?")


def abstract_sop(text: str, deny_list: list[str] | None = None) -> str:
    """Strip tenant-identifying substrings from an SOP text.

    Args:
        text: The raw SOP text (``Skill.sop_text``) to redact.
        deny_list: Additional tokens to treat as repo/owner names when
            scrubbing file paths. The canonical use is to pass the set
            of repo slugs that contributed to the promotion so paths
            like ``src/acme/widget/main.py`` are caught even when the
            plain slug ``acme/widget`` isn't a literal substring.

    Returns:
        The redacted text. Empty input returns empty output; no
        placeholders are ever inserted into an empty string.

    The four identifier classes stripped:

    1. Paths containing any deny_list token → ``<path>``.
    2. Repo slugs matching ``owner/name`` → ``<repo>``.
    3. GitHub handles ``@login`` → ``<user>``.
    4. PR / issue refs ``#N`` → ``<ref>``.
    """
    if not text:
        return ""

    redacted = text

    # 1. File paths that embed any deny_list token. Done first so the
    #    remaining passes don't scrub the token in isolation and leave
    #    the surrounding path fragments behind.
    if deny_list:
        # Deduplicate + sort by length descending so more-specific
        # tokens ("acme/widget") are tried before broader ones ("acme").
        seen: set[str] = set()
        tokens = []
        for raw in deny_list:
            token = raw.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        tokens.sort(key=len, reverse=True)
        for token in tokens:
            redacted = _path_pattern_for(token).sub("<path>", redacted)

    # 2. Canonical owner/name repo slugs.
    redacted = _REPO_SLUG.sub("<repo>", redacted)

    # 3. GitHub handles.
    redacted = _USER_HANDLE.sub("<user>", redacted)

    # 4. PR / issue references.
    redacted = _REF.sub("<ref>", redacted)

    return redacted


__all__ = ["abstract_sop"]
