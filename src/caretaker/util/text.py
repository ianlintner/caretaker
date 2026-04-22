"""Text-munging helpers shared by writers that land files in consumer repos.

The helpers here are intentionally narrow and dependency-free so they can be
called from any file-writing path (Foundry tool registry, GitHub contents
API wrapper, local release-sync templater, etc.) without pulling in agent
scaffolding.
"""

from __future__ import annotations


def ensure_trailing_newline(content: str) -> str:
    """Return ``content`` with exactly one trailing ``\\n`` appended, if missing.

    Why this exists:
        Consumer repos commonly wire the pre-commit ``end-of-file-fixer`` hook,
        which refuses to merge a file that lacks a single trailing newline.
        When caretaker writes templated files (e.g. ``.github/workflows/
        maintainer.yml``) into a consumer repo on version upgrade, a missing
        EOF newline causes the hook to fail, the devops agent then opens a
        CI-failure issue, Copilot is assigned to add the newline, and those
        secondary "add EOF newline" PRs pile up unmerged. Three consumer
        repos hit this exact chain within 48 hours (python_dsa #39/#42/#43,
        kubernetes-apply-vscode #17, flashcards).

    Semantics:
        - If ``content`` ends with ``\\n``, it is returned unchanged — even
          if it already ends with multiple newlines. Over-normalising would
          corrupt Markdown fixtures or JSON payloads where the author
          deliberately left blank trailing lines.
        - If ``content`` is the empty string, the empty string is returned
          unchanged. Callers that want an empty-file-with-newline should
          pass ``"\\n"`` explicitly; we refuse to invent content for them.
        - Otherwise a single ``\\n`` is appended.

    Args:
        content: The text about to be written to disk or sent to the
            GitHub contents API.

    Returns:
        ``content`` with a guaranteed single trailing newline (or the
        original string if it already ended with one, or was empty).
    """
    if not content:
        return content
    if content.endswith("\n"):
        return content
    return content + "\n"
