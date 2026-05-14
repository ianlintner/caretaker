"""Tests for openclaw_http backend registration in handoff_reviewer and auto_fix.

Coverage:
  * _build_specs() / get_spec() includes "openclaw_http"
  * _resolve_backend_module() returns the openclaw_http module (with fix_run)
  * Default PRReviewerConfig does NOT enable "openclaw_http" (opt-in only)
"""

from __future__ import annotations


def test_build_specs_includes_openclaw_http() -> None:
    """get_spec('openclaw_http') must return a spec without raising ValueError."""
    from caretaker.pr_reviewer.handoff_reviewer import get_spec

    spec = get_spec("openclaw_http")
    assert spec.backend == "openclaw_http"


def test_resolve_backend_module_returns_openclaw_http() -> None:
    """_resolve_backend_module('openclaw_http') must return the module and expose fix_run."""
    from caretaker.pr_reviewer.auto_fix import _resolve_backend_module

    module = _resolve_backend_module("openclaw_http")
    assert module is not None
    assert hasattr(module, "fix_run"), "openclaw_http module must expose fix_run()"


def test_openclaw_http_not_in_default_enabled_backends() -> None:
    """openclaw_http is opt-in: it must not appear in the default enabled_backends list."""
    from caretaker.config import PRReviewerConfig

    cfg = PRReviewerConfig()
    assert "openclaw_http" not in cfg.enabled_backends
