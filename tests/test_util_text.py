"""Tests for :mod:`caretaker.util.text`.

Golden-file regression for the release-sync EOF-newline fix.

Context:
    Consumer repos run the ``end-of-file-fixer`` pre-commit hook. When
    caretaker wrote templated files (``maintainer.yml``, agent markdown,
    config YAML, etc.) without a trailing newline, the hook failed and
    triggered a cascade: devops_agent filed a CI-failure issue → Copilot
    opened a "add EOF newline" PR → that PR sat unmerged. Seen in
    python_dsa PRs #39/#42/#43, kubernetes-apply-vscode #17, flashcards.

These tests lock the helper's contract so that regression can't recur:
    * missing newline → exactly one is appended
    * single trailing newline → unchanged
    * triple (or more) trailing newlines → unchanged (do not over-normalise)
    * empty string → unchanged (no invented content)
    * idempotency — helper is safe to call twice
"""

from __future__ import annotations

import pytest

from caretaker.util.text import ensure_trailing_newline


class TestEnsureTrailingNewline:
    def test_appends_newline_when_missing(self) -> None:
        result = ensure_trailing_newline("on: push\njobs: {}")
        assert result == "on: push\njobs: {}\n"
        assert result.endswith("\n")
        # Exactly one trailing newline — not two.
        assert not result.endswith("\n\n")

    def test_leaves_single_trailing_newline_alone(self) -> None:
        content = "on: push\njobs: {}\n"
        assert ensure_trailing_newline(content) == content

    def test_preserves_multiple_trailing_newlines(self) -> None:
        """If the caller deliberately left extra blank lines (e.g. a JSON
        payload or a markdown block with a trailing blank), the helper must
        not over-normalise to a single newline. That would corrupt fixtures.
        """
        content = "hello\n\n\n"
        assert ensure_trailing_newline(content) == content

    def test_empty_string_is_unchanged(self) -> None:
        """We refuse to invent content: empty in, empty out. Callers that
        want an empty-file-with-newline must pass ``"\\n"`` themselves.
        """
        assert ensure_trailing_newline("") == ""

    def test_single_newline_is_unchanged(self) -> None:
        assert ensure_trailing_newline("\n") == "\n"

    def test_is_idempotent(self) -> None:
        """Helper must be safe to call multiple times along a pipeline."""
        once = ensure_trailing_newline("content without newline")
        twice = ensure_trailing_newline(once)
        assert once == twice == "content without newline\n"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Golden fixtures mirroring the real consumer files that regressed.
            ("name: Maintainer\non: schedule", "name: Maintainer\non: schedule\n"),
            ("0.12.1", "0.12.1\n"),  # .github/maintainer/.version
            ("# CHANGELOG\n\n## [2026-W17]\n- x", "# CHANGELOG\n\n## [2026-W17]\n- x\n"),
            ("caretaker:\n  enabled: true\n", "caretaker:\n  enabled: true\n"),
        ],
    )
    def test_golden_fixtures(self, raw: str, expected: str) -> None:
        assert ensure_trailing_newline(raw) == expected
        # Every golden fixture must end with exactly one ``\n`` after the
        # round-trip — this is the invariant the consumer's
        # ``end-of-file-fixer`` hook checks.
        out = ensure_trailing_newline(raw)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_trailing_whitespace_without_newline_still_gets_newline(self) -> None:
        """Trailing spaces are not a newline. A ``.version`` file like
        ``"0.12.1  "`` still needs a ``\\n`` appended — we do not strip the
        spaces (that's a separate linter's job) but we do terminate the line.
        """
        result = ensure_trailing_newline("0.12.1  ")
        assert result == "0.12.1  \n"
