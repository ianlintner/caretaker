"""Guardrails — unified input sanitization, output filtering, and rollback.

This package consolidates the safety primitives the Agentic Design Patterns
Ch. 18 guardrails chapter prescribes into one module so every external-
input boundary and every outbound GitHub write goes through the same
policy, emits the same metrics, and records the same audit trail.

Public surface
--------------

* :func:`sanitize_input` — scrub prompt-injection sigils, normalise Unicode,
  truncate to per-source byte budgets before external content is ever fed
  to an LLM prompt.
* :func:`filter_output` — block LLM outputs that echo injection payloads,
  attempt caretaker-reserved markers, or exceed per-target length caps
  before they reach the GitHub API.
* :func:`checkpoint_and_rollback` — wrap a post-merge state mutation so
  that, if ``verify()`` fails inside the post-action window,
  ``rollback()`` fires automatically.
* :class:`GuardrailsConfig` — config surface bolted onto
  :class:`caretaker.config.MaintainerConfig` with safe defaults
  (enabled=True, strict_mode=False).

The package is deliberately **import-safe with no side effects** — it can
be imported from the hot path without booting the LLM, the GitHub client,
or the graph writer. Every helper is synchronous where the caller can
afford to be synchronous (sanitize / filter) and async where it must own
a window of time (rollback).
"""

from __future__ import annotations

from caretaker.guardrails.filter import (
    FilteredOutput,
    OutputTarget,
    filter_output,
)
from caretaker.guardrails.policy import (
    GuardrailsConfig,
    MergeRollbackConfig,
    OutputPolicy,
    default_policies,
)
from caretaker.guardrails.rollback import (
    CheckpointedAction,
    RollbackOutcome,
    checkpoint_and_rollback,
)
from caretaker.guardrails.sanitize import (
    InputSource,
    Modification,
    ModificationType,
    SanitizedInput,
    sanitize_input,
)

__all__ = [
    "CheckpointedAction",
    "FilteredOutput",
    "GuardrailsConfig",
    "InputSource",
    "MergeRollbackConfig",
    "Modification",
    "ModificationType",
    "OutputPolicy",
    "OutputTarget",
    "RollbackOutcome",
    "SanitizedInput",
    "checkpoint_and_rollback",
    "default_policies",
    "filter_output",
    "sanitize_input",
]
