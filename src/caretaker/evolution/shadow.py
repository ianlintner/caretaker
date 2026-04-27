"""Shadow-mode infrastructure for Phase 2 LLM decision migrations.

This module exists so every Phase 2 LLM handover (readiness, CI triage,
review classification, issue triage, cascade cleanup, stuck-PR detection,
bot identity, dispatch guard, executor routing, crystallizer category)
can be rolled out behind a uniform three-mode switch:

* ``off`` — the classic heuristic is the sole authority; the LLM path
  is never even called.
* ``shadow`` — both paths execute side-by-side. The legacy verdict is
  always returned to the caller (so behaviour is unchanged), but when
  the two verdicts disagree we persist a :class:`ShadowDecisionRecord`
  so operators can inspect the disagreement rate before flipping
  authority.
* ``enforce`` — the candidate is authoritative. Legacy runs only as a
  safety net when the candidate errors or returns ``None``.

The public surface is small by design:

* :func:`shadow_decision` — the decorator call sites use.
* :class:`ShadowDecisionRecord` — the pydantic model persisted to
  Neo4j / the in-memory ring-buffer / the log fallback.
* :func:`recent_records` / :func:`clear_records_for_tests` — the
  in-memory ring buffer used by the admin endpoint and tests.

Concurrent-caller-safety: the ring-buffer and counters are guarded by
module-level :class:`threading.Lock` so multiple request handlers
emitting shadow records in parallel do not corrupt each other's state.
The Neo4j write path piggy-backs on
:class:`caretaker.graph.writer.GraphWriter`, which already serialises
writes through its own background drain task, so we do not need a
second lock there.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import threading
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import (
    Any,
    Literal,
    TypeVar,
    cast,
)

from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field

from caretaker.observability.metrics import REGISTRY, get_service_label

logger = logging.getLogger(__name__)


# ── Types ────────────────────────────────────────────────────────────────

T = TypeVar("T")

ShadowMode = Literal["off", "shadow", "enforce"]
"""One of the three supported modes.

Kept as a :data:`Literal` alias (rather than a :class:`StrEnum`) so the
config surface can reuse the identical string literal without a second
type marshalling step."""

ShadowOutcome = Literal[
    "agree",
    "disagree",
    "candidate_error",
    "legacy_only",
    "enforced_candidate",
]
"""The five outcomes surfaced to Prometheus + the :class:`ShadowDecisionRecord`.

* ``agree`` / ``disagree`` — both paths ran, the legacy verdict was
  returned, and the comparison agreed/disagreed.
* ``candidate_error`` — candidate raised; legacy verdict returned.
* ``legacy_only`` — ``mode == "off"`` so only legacy ran. No record
  is written for this outcome (see :func:`_should_persist`); the
  counter exists purely so dashboards can prove the mode switch is
  live.
* ``enforced_candidate`` — ``mode == "enforce"`` and the candidate
  succeeded; no legacy comparison happened.
"""


# ── Prometheus counter ───────────────────────────────────────────────────
#
# Cardinality analysis: ``name`` is a bounded enum governed by
# :class:`~caretaker.config.AgenticConfig`'s fields (currently 10);
# ``mode`` is 3; ``outcome`` is 5. Upper bound: 150 series, well under
# the cardinality budget in the Prometheus SKILL.

SHADOW_DECISIONS_TOTAL = Counter(
    "caretaker_shadow_decisions_total",
    "Total shadow-mode decisions executed per (name, mode, outcome).",
    ["name", "mode", "outcome"],
    registry=REGISTRY,
)


# ── Ring buffer + record model ───────────────────────────────────────────

_MAX_RECORDS = 1000

_records_lock = threading.Lock()
_records: deque[ShadowDecisionRecord] = deque(maxlen=_MAX_RECORDS)


class ShadowDecisionRecord(BaseModel):
    """A single shadow-mode decision, persisted to Neo4j or logs.

    Matches the ``:ShadowDecision`` node schema in §4.5 of the
    2026-Q2 agentic migration plan. The JSON-serialised verdict fields
    keep the node property set scalar-only, which is both Neo4j-friendly
    and keeps the Prometheus label set small.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description=(
            "Stable unique id so the Neo4j node can be MERGE'd without "
            "writing the same record twice. Generated when the record is "
            "built, so even a transient Neo4j outage that forces a fall-"
            "through to the log path produces a record reconstructable "
            "from logs alone."
        )
    )
    name: str = Field(description="Decision site name (matches AgenticConfig field).")
    repo_slug: str = Field(
        default="",
        description=(
            "Best-effort ``owner/repo`` attribution. Callers pass this in "
            "via the ``context`` dict under the ``repo_slug`` key; empty "
            "string when unknown."
        ),
    )
    run_at: datetime = Field(description="When the shadow decision was made (tz-aware UTC).")
    outcome: ShadowOutcome = Field(
        description="Comparison outcome. ``legacy_only`` is filtered out before persisting.",
    )
    mode: ShadowMode = Field(description="The mode that was active when the decision ran.")
    legacy_verdict_json: str = Field(
        description="JSON-serialised legacy verdict. Always populated.",
    )
    candidate_verdict_json: str | None = Field(
        default=None,
        description="JSON-serialised candidate verdict, or ``None`` on candidate_error.",
    )
    disagreement_reason: str | None = Field(
        default=None,
        description="One-line human summary of the disagreement, if known.",
    )
    context_json: str = Field(
        default="{}",
        description="JSON-serialised caller context (repo, pr number, etc.).",
    )
    # Per-PR #503 (nightly-eval harness): capture which model produced the
    # legacy vs candidate verdict so paired Braintrust experiments can
    # disambiguate a prompt-change regression from a model-swap regression.
    # Both default to ``None`` so rows written before these fields existed
    # load cleanly — Neo4j nodes persisted pre-field lack the property and
    # pydantic's ``extra='forbid'`` on StrictBaseModel doesn't apply here.
    legacy_model: str | None = Field(
        default=None,
        description=(
            "Model used by the legacy leg, if any. ``None`` when the legacy "
            "leg is a pure heuristic (the usual case) — most sites only set "
            "``candidate_model`` because only the candidate leg calls an LLM."
        ),
    )
    candidate_model: str | None = Field(
        default=None,
        description=(
            "Model used by the candidate leg. Equals the site's "
            "``AgenticDomainConfig.model_override`` when set, else the "
            "router's ``llm.default_model`` at the time of the call. "
            "``None`` when the decorator was unable to determine a model "
            "(e.g. candidate errored before issuing an LLM request)."
        ),
    )


# ── Graph-store write helper ─────────────────────────────────────────────


def write_shadow_decision(record: ShadowDecisionRecord) -> None:
    """Persist one ``:ShadowDecision`` record.

    Single code path that chooses Neo4j vs a structured log line based on
    whether :class:`~caretaker.graph.writer.GraphWriter` is enabled. The
    record must be reconstructable from the log line alone, so every
    field is emitted — truncated or not — in a stable ``shadow_decision``
    event body.

    Also appends to the in-memory ring buffer regardless of the backend:
    the admin endpoint uses the ring as the dev-mode fallback and tests
    assert on it without needing a live Neo4j.
    """
    # Ring buffer first: that is what the admin endpoint reads in dev,
    # and it is the only path that is always on.
    with _records_lock:
        _records.append(record)

    # Graph writer: no-op when Neo4j is not configured.
    from caretaker.graph.writer import get_writer

    writer = get_writer()
    properties: dict[str, Any] = {
        "name": record.name,
        "repo_slug": record.repo_slug,
        "run_at": record.run_at.isoformat(),
        "outcome": record.outcome,
        "mode": record.mode,
        "legacy_verdict_json": record.legacy_verdict_json,
        "candidate_verdict_json": record.candidate_verdict_json or "",
        "disagreement_reason": record.disagreement_reason or "",
        "context_json": record.context_json,
        # Empty string rather than None so the Neo4j storage layer doesn't
        # have to special-case ``NULL`` property values; the admin API
        # normalises the empty-string back to ``None`` on read.
        "legacy_model": record.legacy_model or "",
        "candidate_model": record.candidate_model or "",
    }
    stats = writer.stats()
    if stats.get("enabled"):
        writer.record_node("ShadowDecision", record.id, properties)
    else:
        # Structured log line — reconstructable. The ``event`` key is how
        # the log pipeline routes it; downstream tooling can pick up any
        # record by ``id`` alone.
        logger.info(
            "shadow_decision event=shadow_decision id=%s name=%s outcome=%s "
            "mode=%s repo=%s payload=%s",
            record.id,
            record.name,
            record.outcome,
            record.mode,
            record.repo_slug,
            json.dumps(properties, default=str, sort_keys=True),
        )


def recent_records(
    *, name: str | None = None, since: datetime | None = None, limit: int = 100
) -> list[ShadowDecisionRecord]:
    """Return the most-recent shadow-decision records (newest-first).

    Reads the in-memory ring buffer; used by the admin endpoint's dev
    fallback path when Neo4j is disabled.
    """
    with _records_lock:
        snapshot = list(_records)
    # Newest-first; deque is append-right so reverse.
    filtered: list[ShadowDecisionRecord] = []
    for rec in reversed(snapshot):
        if name is not None and rec.name != name:
            continue
        if since is not None and rec.run_at < since:
            continue
        filtered.append(rec)
        if len(filtered) >= limit:
            break
    return filtered


def clear_records_for_tests() -> None:
    """Drop the in-memory ring buffer. Used by tests between cases."""
    with _records_lock:
        _records.clear()


# ── Decorator ────────────────────────────────────────────────────────────
#
# The decorator wraps an async function with the signature
#
#     async def decide(*args, legacy, candidate, **kwargs) -> T:
#
# and returns a same-signature callable that dispatches on
# ``config.agentic.<name>.mode``. Callers provide both implementations
# explicitly — no global registry — so migrations can be done in a
# single PR without a cross-module import spaghetti.


# Callables the decorator dispatches through. Typed as ``Any`` because
# call sites hand in implementations with business-specific signatures
# that vary per decision site — a Protocol would impose more structure
# than the runtime needs and ``Any`` keeps call sites from fighting the
# type checker.
_LegacyFn = Callable[..., Any]
_CandidateFn = Callable[..., Any]


def _resolve_mode(name: str) -> ShadowMode:
    """Resolve the currently configured mode for ``name``.

    Uses :func:`caretaker.evolution.shadow_config.get_active_config` which
    caretaker wires at orchestrator startup. Falls back to ``"off"`` when
    no config is installed, which matches the default-safe behaviour of
    every other Phase 2 knob: unconfigured means classic heuristics
    stay authoritative.
    """
    from caretaker.evolution import shadow_config

    cfg = shadow_config.get_active_config()
    if cfg is None:
        return "off"
    domain = getattr(cfg, name, None)
    if domain is None:
        return "off"
    mode = getattr(domain, "mode", "off")
    if mode not in ("off", "shadow", "enforce"):
        return "off"
    return cast("ShadowMode", mode)


def _resolve_model_overrides(name: str) -> tuple[str | None, int | None]:
    """Return ``(model_override, max_tokens_override)`` for ``name``.

    Both are ``None`` when no :class:`~caretaker.config.AgenticConfig` is
    installed or the domain has no override configured. The decorator
    threads a non-``None`` ``model_override`` into the candidate call as
    a ``model=`` kwarg so the candidate's LLM call picks it up instead
    of the router's ``default_model``.
    """
    from caretaker.evolution import shadow_config

    cfg = shadow_config.get_active_config()
    if cfg is None:
        return None, None
    domain = getattr(cfg, name, None)
    if domain is None:
        return None, None
    model_override = getattr(domain, "model_override", None)
    max_tokens_override = getattr(domain, "max_tokens_override", None)
    return model_override, max_tokens_override


def _resolve_default_model() -> str | None:
    """Return the router's ``llm.default_model``, if a config is installed.

    Used to stamp :class:`ShadowDecisionRecord.candidate_model` (and
    ``legacy_model`` when the legacy leg itself is LLM-backed — rare, but
    possible on sites like ``dispatch_guard`` where the ``legacy``
    heuristic is itself a regex-then-LLM cascade). ``None`` when no
    ``MaintainerConfig`` is reachable from the active shadow config — the
    :class:`AgenticConfig` resolver doesn't carry a backref to the
    parent, so this helper reads the full config resolver registered by
    the admin backend / orchestrator.
    """
    from caretaker.evolution import shadow_config

    cfg_getter = getattr(shadow_config, "get_active_maintainer_config", None)
    if cfg_getter is None:
        return None
    maintainer = cfg_getter()
    if maintainer is None:
        return None
    llm_cfg = getattr(maintainer, "llm", None)
    if llm_cfg is None:
        return None
    default_model = getattr(llm_cfg, "default_model", None)
    return default_model if isinstance(default_model, str) else None


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise return it unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


def _serialise_verdict(verdict: Any) -> str:
    """Best-effort JSON serialisation for a verdict (pydantic-aware)."""
    if verdict is None:
        return "null"
    if isinstance(verdict, BaseModel):
        return verdict.model_dump_json()
    try:
        return json.dumps(verdict, default=str, sort_keys=True)
    except (TypeError, ValueError):
        # Fall back to ``repr`` — the audit trail is more important than
        # perfect JSON. The record is still reconstructable from logs.
        return json.dumps({"repr": repr(verdict)})


def _serialise_context(context: dict[str, Any] | None) -> str:
    if not context:
        return "{}"
    try:
        return json.dumps(context, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps({"repr": repr(context)})


def _default_compare(a: Any, b: Any) -> bool:
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 — exotic __eq__ that raises
        return False


def shadow_decision(
    name: str,
    *,
    compare: Callable[[Any, Any], bool] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorate a decision site to run legacy + candidate side-by-side.

    The decorated function must be ``async`` and must accept ``legacy``
    and ``candidate`` keyword-only (or keyword-passable) arguments so
    call sites are the ones supplying both implementations. The
    decorator itself never imports anything from the caller's module.

    Parameters
    ----------
    name:
        Matches an :class:`~caretaker.config.AgenticDomainConfig` field
        on :class:`~caretaker.config.AgenticConfig`. The mode comes from
        ``config.agentic.<name>.mode`` — unknown names silently default
        to ``off``.
    compare:
        Predicate used to decide whether two verdicts agree. Defaults to
        ``==``. Provide a custom one when verdicts carry noisy fields
        (e.g. timestamps, rationale strings) that should not count as
        disagreements.

    The decorator always returns the legacy verdict in ``shadow`` mode
    so the existing behaviour is byte-identical to the non-migrated
    world. Candidate errors in ``shadow`` mode are swallowed and
    recorded; the legacy path is never affected by a candidate failure.
    """
    compare_fn = compare or _default_compare

    def decorator(
        func: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"@shadow_decision requires an async function; got {func!r}.")

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            legacy: _LegacyFn | None = kwargs.pop("legacy", None)
            candidate: _CandidateFn | None = kwargs.pop("candidate", None)
            # ``context`` is a control-plane kwarg used only to attribute
            # the record; it is *not* forwarded to the legacy / candidate
            # callables. Use ``pop`` so the downstream functions don't
            # see an unexpected keyword.
            context = kwargs.pop("context", None)
            if legacy is None or candidate is None:
                raise TypeError(
                    "@shadow_decision-wrapped callable must be invoked with "
                    "'legacy' and 'candidate' keyword arguments."
                )

            mode = _resolve_mode(name)
            # Per-site model override (PR #503 follow-up). When set, the
            # candidate leg receives a ``model=<override>`` kwarg so its
            # LLM call resolves through ``ClaudeClient.structured_complete``
            # / ``.complete`` with the override rather than the router's
            # ``default_model``. The legacy leg is never given this kwarg —
            # the legacy function signature must stay unchanged.
            model_override, max_tokens_override = _resolve_model_overrides(name)
            default_model = _resolve_default_model()
            candidate_model = model_override or default_model
            # ``service`` is captured for metric label consistency but is
            # not used here; the Counter already pins the label set and
            # standard ``logging`` would reject an ``extra={"name": ...}``
            # kwarg because ``name`` is a reserved :class:`LogRecord`
            # attribute.
            _ = get_service_label()

            def _candidate_kwargs() -> dict[str, Any]:
                """Inject ``model`` / ``max_tokens`` override kwargs for the candidate.

                We copy ``kwargs`` and only add the override keys when the
                override is set so the candidate signature doesn't have
                to declare them (it only needs to accept ``**kwargs``).
                Sites that don't call an LLM in their candidate (or that
                already hardcode a specific model) simply drop these
                kwargs without any runtime cost.
                """
                extra = dict(kwargs)
                if model_override is not None:
                    extra["model"] = model_override
                if max_tokens_override is not None:
                    extra["max_tokens"] = max_tokens_override
                return extra

            # ── off ────────────────────────────────────────────────
            if mode == "off":
                SHADOW_DECISIONS_TOTAL.labels(name=name, mode=mode, outcome="legacy_only").inc()
                return cast("T", await _maybe_await(legacy(*args, **kwargs)))

            # ── enforce ────────────────────────────────────────────
            if mode == "enforce":
                try:
                    candidate_result = await _maybe_await(candidate(*args, **_candidate_kwargs()))
                except Exception as exc:  # noqa: BLE001 — enforce must fall through
                    logger.warning(
                        "shadow_decision enforce: candidate raised for name=%s; "
                        "falling through to legacy (%s: %s)",
                        name,
                        type(exc).__name__,
                        exc,
                    )
                    SHADOW_DECISIONS_TOTAL.labels(
                        name=name, mode=mode, outcome="candidate_error"
                    ).inc()
                    return cast("T", await _maybe_await(legacy(*args, **kwargs)))

                if candidate_result is None:
                    logger.warning(
                        "shadow_decision enforce: candidate returned None for "
                        "name=%s; falling through to legacy",
                        name,
                    )
                    SHADOW_DECISIONS_TOTAL.labels(
                        name=name, mode=mode, outcome="candidate_error"
                    ).inc()
                    return cast("T", await _maybe_await(legacy(*args, **kwargs)))

                SHADOW_DECISIONS_TOTAL.labels(
                    name=name, mode=mode, outcome="enforced_candidate"
                ).inc()
                return cast("T", candidate_result)

            # ── shadow ─────────────────────────────────────────────
            # Legacy first so a candidate failure never blocks the hot path.
            legacy_result = await _maybe_await(legacy(*args, **kwargs))

            candidate_verdict: Any = None
            candidate_error: BaseException | None = None
            try:
                candidate_verdict = await _maybe_await(candidate(*args, **_candidate_kwargs()))
            except Exception as exc:  # noqa: BLE001 — shadow must swallow
                candidate_error = exc

            now = datetime.now(UTC)
            repo_slug = ""
            if isinstance(context, dict):
                raw_repo = context.get("repo_slug", "")
                repo_slug = str(raw_repo) if raw_repo is not None else ""

            ctx_dict = context if isinstance(context, dict) else None

            if candidate_error is not None:
                err_reason = f"candidate_error: {type(candidate_error).__name__}: {candidate_error}"
                err_record = ShadowDecisionRecord(
                    id=str(uuid.uuid4()),
                    name=name,
                    repo_slug=repo_slug,
                    run_at=now,
                    outcome="candidate_error",
                    mode=mode,
                    legacy_verdict_json=_serialise_verdict(legacy_result),
                    candidate_verdict_json=None,
                    disagreement_reason=err_reason,
                    context_json=_serialise_context(ctx_dict),
                    legacy_model=default_model,
                    candidate_model=candidate_model,
                )
                write_shadow_decision(err_record)
                SHADOW_DECISIONS_TOTAL.labels(name=name, mode=mode, outcome="candidate_error").inc()
                return cast("T", legacy_result)

            agreed = compare_fn(legacy_result, candidate_verdict)
            outcome: ShadowOutcome = "agree" if agreed else "disagree"
            reason: str | None = (
                None
                if agreed
                else (
                    f"legacy={_short_repr(legacy_result)} "
                    f"!= candidate={_short_repr(candidate_verdict)}"
                )
            )
            record = ShadowDecisionRecord(
                id=str(uuid.uuid4()),
                name=name,
                repo_slug=repo_slug,
                run_at=now,
                outcome=outcome,
                mode=mode,
                legacy_verdict_json=_serialise_verdict(legacy_result),
                candidate_verdict_json=_serialise_verdict(candidate_verdict),
                disagreement_reason=reason,
                context_json=_serialise_context(ctx_dict),
                legacy_model=default_model,
                candidate_model=candidate_model,
            )
            write_shadow_decision(record)
            SHADOW_DECISIONS_TOTAL.labels(name=name, mode=mode, outcome=outcome).inc()
            return cast("T", legacy_result)

        return wrapper

    return decorator


def _short_repr(value: Any, *, limit: int = 120) -> str:
    """Truncate a ``repr`` so a disagreement reason fits in one log line."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001
        text = f"<unreprable {type(value).__name__}>"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


__all__ = [
    "SHADOW_DECISIONS_TOTAL",
    "ShadowDecisionRecord",
    "ShadowMode",
    "ShadowOutcome",
    "clear_records_for_tests",
    "recent_records",
    "shadow_decision",
    "write_shadow_decision",
]
