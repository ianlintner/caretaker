"""Guardrails policy models — defaults for sanitize + filter + rollback.

These are the knobs operators can tune without touching code. The pydantic
models live here (rather than in ``caretaker.config``) so the guardrails
package can be imported without pulling the whole config surface with it;
``MaintainerConfig`` re-exports them via a thin ``GuardrailsConfig`` field.

Design notes
------------

* Every output target has a ``max_length`` cap — GitHub imposes its own
  limits (comment bodies up to 65536 chars, check-run output up to
  65535) but we cap well below so downstream render paths (status
  comment composer, embedding generator) keep predictable budgets.
* ``block_caretaker_markers`` defaults to ``True`` on every target. The
  ``<!-- caretaker:* -->`` HTML-comment markers are reserved for
  caretaker's own state tracking; letting an LLM emit one opens a
  dispatch-guard bypass.
* ``MergeRollbackConfig.enabled`` defaults to ``False`` on first ship —
  operators promote per-repo once they are comfortable with the
  post-merge verify cadence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputPolicy(_StrictModel):
    """Policy applied to every outbound LLM-authored write.

    Attributes
    ----------
    max_length
        Hard cap on the filtered content length (chars). Over-cap content
        is truncated with a trailing ``"... [truncated by guardrails]"``
        marker so reviewers can tell the content was clipped (vs the
        model simply generating a short reply).
    block_caretaker_markers
        When ``True`` (default), any ``<!-- caretaker:* -->`` HTML-comment
        in the LLM output is stripped. Operators should never turn this
        off — the markers are reserved for caretaker's dispatch-guard.
    block_hidden_links
        Strip zero-width characters and markdown links whose visible text
        and target URL disagree by more than a conservative threshold.
        These are classic spear-phishing patterns that the LLM can pick
        up from a compromised upstream issue body.
    block_shell_escapes
        Strip ANSI escape sequences (``\\x1b[…``) that occasionally leak
        from CI log echoes; rendering them in a GitHub comment is a
        minor phishing surface for terminal readers.
    echo_sigils
        When ``True`` (default), outputs that contain known prompt-
        injection sigils (from the same list the sanitizer uses) are
        blocked. This catches the "echo attack" where the LLM parrots
        an injection string from a poisoned input back into a PR body.
    """

    max_length: int = 16384
    # Marker-stripping is off by default at the GitHub-client boundary
    # because caretaker's own status-comment composer legitimately emits
    # ``<!-- caretaker:status -->`` markers. Call sites that know they
    # are handling LLM-authored content (where the LLM should never be
    # able to emit a marker) should pass a tuned policy with
    # ``block_caretaker_markers=True``.
    block_caretaker_markers: bool = False
    block_hidden_links: bool = True
    block_shell_escapes: bool = True
    echo_sigils: bool = True


def default_policies() -> dict[str, OutputPolicy]:
    """Built-in per-target defaults.

    Keys match :class:`caretaker.guardrails.filter.OutputTarget` so the
    dictionary can be consumed without a separate mapping step.
    """
    return {
        "github_comment": OutputPolicy(max_length=16384),
        "github_pr_body": OutputPolicy(max_length=32768),
        "github_issue_body": OutputPolicy(max_length=32768),
        "check_run_output": OutputPolicy(max_length=32768),
    }


class MergeRollbackConfig(_StrictModel):
    """Post-merge rollback window configuration.

    Attributes
    ----------
    enabled
        Off by default on first ship — operators promote per-repo once
        they are comfortable with the 5-minute hold. When ``False``,
        :func:`caretaker.pr_agent.merge.perform_merge` skips the
        :func:`caretaker.guardrails.checkpoint_and_rollback` wrapper and
        returns as soon as the merge API call completes.
    window_seconds
        How long to poll base-branch CI after a merge before declaring
        the merge healthy. 300 s = 5 minutes mirrors the Ch. 18 "5-minute
        rollback window" rule-of-thumb; longer windows miss the
        post-deploy soak; shorter windows flag too many transient CI
        hiccups as rollbacks.
    poll_interval_seconds
        Time between CI polls inside the window. 15 s is slow enough to
        stay under the GitHub rate-limit budget for a single merge and
        fast enough to catch an immediate red post-merge build.
    """

    enabled: bool = False
    window_seconds: int = 300
    poll_interval_seconds: int = 15


class GuardrailsConfig(_StrictModel):
    """Operator-facing guardrails configuration.

    This is what lands on :attr:`caretaker.config.MaintainerConfig.guardrails`
    with safe defaults so the feature is live from day one without a YAML
    change. Operators who want to tune the per-target caps or ship a
    custom sigil list override the relevant fields.
    """

    enabled: bool = True
    strict_mode: bool = False
    sigil_list_path: str | None = None
    output_policies: dict[str, OutputPolicy] = Field(default_factory=default_policies)


__all__ = [
    "GuardrailsConfig",
    "MergeRollbackConfig",
    "OutputPolicy",
    "default_policies",
]
