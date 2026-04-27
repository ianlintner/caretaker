"""Regression guard: ``datetime.utcnow()`` must not return to ``src/caretaker``.

``datetime.utcnow()`` is deprecated in Python 3.12 and produces naive
datetimes that mix unsafely with the tz-aware datetimes used elsewhere in
the codebase (``last_copilot_attempt_at``, fleet heartbeat timestamps,
graph node ``write_ts``). The sweep PR replaced every call site with
``datetime.now(UTC)``; this test stops it creeping back.

The check walks the AST rather than doing a substring scan so references
inside docstrings, comments, or string literals do not trigger false
positives.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "caretaker"


def _call_is_datetime_utcnow(node: ast.Call) -> bool:
    """Return True if ``node`` is a call like ``datetime.utcnow()``.

    Matches both ``datetime.utcnow()`` (module-level ``datetime`` type) and
    ``datetime.datetime.utcnow()`` (when the module itself is imported as
    ``datetime``). Does not match unrelated ``something.utcnow()`` helpers.
    """
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "utcnow":
        return False
    value = func.value
    # datetime.utcnow()
    if isinstance(value, ast.Name) and value.id == "datetime":
        return True
    # datetime.datetime.utcnow()
    return bool(
        isinstance(value, ast.Attribute)
        and value.attr == "datetime"
        and isinstance(value.value, ast.Name)
        and value.value.id == "datetime"
    )


def test_no_datetime_utcnow_in_src() -> None:
    """No source file under ``src/caretaker/`` may call ``datetime.utcnow()``."""
    offenders: list[str] = []
    for py_file in SRC_ROOT.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_is_datetime_utcnow(node):
                rel = py_file.relative_to(SRC_ROOT.parent.parent)
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "datetime.utcnow() is deprecated and produces naive datetimes. "
        "Use datetime.now(UTC) instead. Offending call sites:\n  " + "\n  ".join(offenders)
    )
