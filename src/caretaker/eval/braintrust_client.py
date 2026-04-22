"""Thin wrapper around the optional ``braintrust`` Python SDK.

Design constraints:

* **Optional extra.** ``braintrust`` is installed under the ``eval``
  extra in ``pyproject.toml``. The harness must keep working when the
  SDK is not importable — we fail open and the caller treats the
  experiment step as a no-op.
* **Fail-open on missing API key.** ``BRAINTRUST_API_KEY`` is read at
  client construction time; absence disables the client rather than
  raising. Local dev and ``--dry-run`` invocations never touch the
  network.
* **Dependency injection.** The harness accepts a :class:`BraintrustClient`
  instance so tests can swap in a fake without patching the real SDK.

The public surface is deliberately narrow: two methods
(``log_experiment`` + ``register_scorer``) plus the module-level
:func:`get_default_client` factory. Everything else routes through the
SDK directly so a future upgrade of the SDK does not require surgery
here.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable  # noqa: TC003 — used at runtime in instance attributes
from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003 — runtime signature of log_experiment
from typing import Any

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────────────


class BraintrustUnavailable(RuntimeError):  # noqa: N818 — intentional public name, external docs
    """Raised when explicit Braintrust access is requested but unavailable.

    The harness does not propagate this — it catches and logs. Tests may
    assert on it to prove the fail-open path is exercised.
    """


# ── Data model ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvalCase:
    """One (input, expected, actual, scores) row handed to Braintrust.

    ``scores`` maps scorer name to a float in ``[0.0, 1.0]``. The wrapper
    normalises booleans to ``1.0``/``0.0`` on ingest so call sites can
    stay agnostic.
    """

    input: dict[str, Any]
    expected: dict[str, Any]
    actual: dict[str, Any]
    scores: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExperimentResult:
    """Return value from :meth:`BraintrustClient.log_experiment`."""

    name: str
    experiment_url: str | None
    case_count: int
    logged: bool
    """``True`` when the SDK + API key were available and the cases were
    uploaded; ``False`` when the client fell open to local-only mode.
    """


# ── Client ───────────────────────────────────────────────────────────────


class BraintrustClient:
    """Dependency-injectable wrapper around ``braintrust``.

    Real usage: construct without arguments so the SDK and env key are
    read from the process environment. Tests: pass ``sdk=FakeSDK()`` to
    inject a stub that records calls.
    """

    def __init__(
        self,
        *,
        project: str = "caretaker-shadow-eval",
        sdk: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self._project = project
        self._api_key = api_key if api_key is not None else os.environ.get("BRAINTRUST_API_KEY")
        self._sdk = sdk if sdk is not None else _import_sdk()
        self._scorers: dict[str, Callable[..., Any]] = {}

    @property
    def available(self) -> bool:
        """``True`` when we have both an SDK and an API key."""
        return self._sdk is not None and bool(self._api_key)

    def register_scorer(self, name: str, scorer_fn: Callable[..., Any]) -> None:
        """Register a scorer callable so it is discoverable by name.

        Registration is a pure local operation — Braintrust's own
        scorer-registration API is SDK-specific and evolves fast; we keep
        a local map so the harness can emit ``scores`` dicts keyed by the
        same names on both the Braintrust upload path and the local
        ``--dry-run`` path.
        """
        if not name:
            raise ValueError("scorer name must be non-empty")
        self._scorers[name] = scorer_fn

    def scorers(self) -> dict[str, Callable[..., Any]]:
        """Snapshot of the registered scorer map. Tests assert on this."""
        return dict(self._scorers)

    def log_experiment(
        self,
        name: str,
        cases: list[EvalCase],
        *,
        run_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExperimentResult:
        """Upload ``cases`` as a single experiment named ``name``.

        Fail-open semantics: when the SDK or API key is missing we log
        once at WARNING and return a result with ``logged=False``. The
        harness treats that as "local-only report" and moves on.
        """
        if not self.available:
            logger.warning(
                "braintrust_unavailable event=braintrust_unavailable project=%s "
                "name=%s case_count=%d reason=%s",
                self._project,
                name,
                len(cases),
                "no_sdk" if self._sdk is None else "no_api_key",
            )
            return ExperimentResult(
                name=name,
                experiment_url=None,
                case_count=len(cases),
                logged=False,
            )

        # The braintrust SDK's API surface has churned; rather than pin a
        # specific call shape, we use getattr-with-fallback so the two
        # historical spellings both work.
        #
        #   braintrust.init_experiment(project=..., name=...) → Experiment
        #   braintrust.Experiment(project=..., name=...)      → Experiment
        #
        # Both expose ``log(input=..., expected=..., output=..., scores=...)``
        # and ``summarize()`` → ``ExperimentSummary`` with an ``experiment_url``.
        try:
            experiment = self._open_experiment(name=name, metadata=metadata or {})
            for case in cases:
                normalized_scores = {k: _coerce_score(v) for k, v in case.scores.items()}
                experiment.log(
                    input=case.input,
                    expected=case.expected,
                    output=case.actual,
                    scores=normalized_scores,
                    metadata=case.metadata,
                )
            summary = experiment.summarize()
            url = _extract_url(summary)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "braintrust_log_failed event=braintrust_log_failed project=%s "
                "name=%s case_count=%d error=%s: %s",
                self._project,
                name,
                len(cases),
                type(exc).__name__,
                exc,
            )
            return ExperimentResult(
                name=name,
                experiment_url=None,
                case_count=len(cases),
                logged=False,
            )

        # The unused ``run_at`` parameter is part of the public API so
        # callers can pin an experiment to a specific evaluation window
        # even when the upload path falls open; swallow it silently here.
        _ = run_at

        return ExperimentResult(
            name=name,
            experiment_url=url,
            case_count=len(cases),
            logged=True,
        )

    # ── Internal ────────────────────────────────────────────────────────

    def _open_experiment(self, *, name: str, metadata: dict[str, Any]) -> Any:
        assert self._sdk is not None  # callers gate on ``available``
        sdk = self._sdk
        init = getattr(sdk, "init_experiment", None)
        if callable(init):
            return init(project=self._project, name=name, metadata=metadata)
        ctor = getattr(sdk, "Experiment", None)
        if callable(ctor):
            return ctor(project=self._project, name=name, metadata=metadata)
        raise BraintrustUnavailable("braintrust SDK exposes neither init_experiment nor Experiment")


# ── Helpers ──────────────────────────────────────────────────────────────


def _import_sdk() -> Any | None:
    """Try to import ``braintrust``; return ``None`` if missing."""
    try:
        import braintrust
    except Exception as exc:  # noqa: BLE001 — missing extra is expected
        logger.debug("braintrust SDK not importable: %s", exc)
        return None
    return braintrust


def _coerce_score(value: Any) -> float:
    """Normalise scorer outputs to a float in ``[0.0, 1.0]``."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def _extract_url(summary: Any) -> str | None:
    """Best-effort extraction of the experiment URL from an SDK summary."""
    for attr in ("experiment_url", "url"):
        url = getattr(summary, attr, None)
        if isinstance(url, str) and url:
            return url
    if isinstance(summary, dict):
        raw = summary.get("experiment_url") or summary.get("url")
        if isinstance(raw, str) and raw:
            return raw
    return None


# ── Module-level default ─────────────────────────────────────────────────

_default: BraintrustClient | None = None


def get_default_client() -> BraintrustClient:
    """Return a process-wide :class:`BraintrustClient`.

    Tests should construct their own instance rather than call this; the
    default is only for the CLI and the admin-endpoint read path.
    """
    global _default  # noqa: PLW0603 — process singleton
    if _default is None:
        _default = BraintrustClient()
    return _default


def reset_default_client_for_tests() -> None:
    """Clear the module-level default client. Used by tests."""
    global _default  # noqa: PLW0603
    _default = None


__all__ = [
    "BraintrustClient",
    "BraintrustUnavailable",
    "EvalCase",
    "ExperimentResult",
    "get_default_client",
    "reset_default_client_for_tests",
]
