"""PRReviewerAgent — dual-path PR code reviewer.

Subscribes to ``pull_request`` opened/synchronize events.  For each new or
updated PR it:

1. Scores the PR using :mod:`routing` (LOC, file count, sensitive patterns, labels).
2. Routes to the inline LLM reviewer  (score < threshold) or
   the ``claude-code-action`` hand-off (score >= threshold).
3. Posts the review via the GitHub Reviews API (inline path) or
   applies a trigger label + hand-off comment (claude-code path).

Enabled by default; set ``pr_reviewer.enabled = false`` to disable.
By default the agent runs on both webhook and polling paths
(``webhook_only = false``). Set ``webhook_only = true`` only if you have
a webhook dispatcher wired up AND want to minimise GitHub REST calls.
Idempotency is provided by ``skip_labels`` (defaults to
``["caretaker:reviewed"]``) rather than a narrow trigger list.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import trace as _otel_trace

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.evolution.executor_routing import (
    ExecutorRoute,
    ExecutorRouteContext,
    ExecutorRouteFile,
    executor_routes_agree,
    route_executor_llm,
    route_from_pr_reviewer_legacy,
)
from caretaker.evolution.shadow import shadow_decision
from caretaker.observability.metrics import (
    observe_pr_review_duration,
    record_pr_review_outcome,
)
from caretaker.pr_reviewer import auto_fix as _auto_fix
from caretaker.pr_reviewer import handoff_review_consumer, handoff_reviewer, inline_reviewer
from caretaker.pr_reviewer.github_review import post_review
from caretaker.pr_reviewer.routing import decide
from caretaker.state.models import TrackedPR

# Module-level tracer — re-used across all PR review handlers.
_tracer = _otel_trace.get_tracer("caretaker.pr_reviewer")

if TYPE_CHECKING:
    from caretaker.pr_reviewer.complexity_classifier import ComplexityTier
    from caretaker.pr_reviewer.inline_reviewer import ReviewResult
    from caretaker.state.models import OrchestratorState


@shadow_decision("executor_routing", compare=executor_routes_agree)
async def _decide_executor_route(
    *, legacy: Any, candidate: Any, context: Any = None
) -> ExecutorRoute:
    """Shadow-mode decision point wrapping the legacy + LLM routing paths.

    The body never runs — the decorator short-circuits to ``legacy`` in
    ``off`` / ``shadow`` modes and to ``candidate`` in ``enforce`` mode.
    """
    raise AssertionError("shadow_decision wrapper short-circuits this placeholder")


logger = logging.getLogger(__name__)

_DEFAULT_HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


def _is_caretaker_owned(pr: dict[str, Any]) -> bool:
    """Return True when the PR was authored by caretaker's bot account."""
    from caretaker.identity.bot import deterministic_family  # noqa: PLC0415

    login = (pr.get("user") or {}).get("login", "")
    return deterministic_family(login) == "caretaker"


# Sentinel passed to local-subprocess runners that have no per-backend
# config block yet (e.g. the greptile stub). Using a frozen empty
# dataclass instance — not ``None`` — so runners can ``getattr`` safely
# while we add config blocks one backend at a time.
_EMPTY_BACKEND_CONFIG = type("_EmptyBackendConfig", (), {})()


@dataclass
class _PRReviewReport:
    reviewed: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # PR numbers where caretaker harvested a hand-off agent's structured
    # review payload and re-posted it as a formal GitHub review (Reviews
    # tab attribution). Distinct from ``reviewed`` (caretaker's own
    # inline LLM review) and ``dispatched`` (initial hand-off comment
    # posted) so the run summary can tell which channel produced the
    # review.
    harvested: list[int] = field(default_factory=list)


class PRReviewerAgent(BaseAgent):
    """Dual-path PR code reviewer."""

    @property
    def name(self) -> str:
        return "pr-reviewer"

    def enabled(self) -> bool:
        return self._ctx.config.pr_reviewer.enabled

    async def execute(
        self,
        state: OrchestratorState,
        event_payload: dict[str, Any] | None = None,
    ) -> AgentResult:
        cfg = self._ctx.config.pr_reviewer
        report = _PRReviewReport()

        # Webhook-only mode: skip entirely when called from a polling run.
        if cfg.webhook_only and not event_payload:
            return AgentResult(processed=0)

        action = (event_payload or {}).get("action", "")
        handled = (
            frozenset(cfg.trigger_actions) if cfg.trigger_actions else _DEFAULT_HANDLED_ACTIONS
        )
        if action and action not in handled:
            return AgentResult(processed=0)

        pr_data = (event_payload or {}).get("pull_request") if event_payload else None

        if pr_data:
            prs = [pr_data]
        else:
            # Polling fallback (only reached when webhook_only=False).
            try:
                prs = await self._ctx.github.list_pull_requests(
                    self._ctx.owner, self._ctx.repo, state="open"
                )
                # Convert to plain dicts so the handler doesn't need branching
                prs = [
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "body": pr.body,
                        "draft": pr.draft,
                        "head": {"sha": pr.head_sha},
                        "labels": [{"name": lbl.name} for lbl in pr.labels],
                    }
                    for pr in prs
                ]
            except Exception as exc:
                err = f"pr-reviewer: failed to list PRs: {exc}"
                logger.error(err)
                report.errors.append(err)
                return AgentResult(errors=report.errors)

        for pr in prs:
            pr_number = int(pr.get("number", 0))
            if not pr_number:
                continue
            try:
                await self._handle_pr(pr, report, state=state)
            except Exception as exc:
                err = f"pr-reviewer: unhandled error on #{pr_number}: {exc}"
                logger.exception(err)
                report.errors.append(err)

        return AgentResult(
            processed=len(report.reviewed) + len(report.dispatched) + len(report.harvested),
            errors=report.errors,
            extra={
                "reviewed": report.reviewed,
                "dispatched": report.dispatched,
                "harvested": report.harvested,
                "skipped": report.skipped,
            },
        )

    def _emit_pr_review_audit(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        pr_author: str,
        is_caretaker_pr: bool,
        routing_reason: str,
        tier: str | None,
        backend: str | None,
        model: str | None,
        verdict: str | None,
        start_monotonic: float,
        auto_fix_dispatched: bool,
        auto_fix_reason: str | None,
        span: Any = None,
    ) -> None:
        """Emit the per-PR audit log line + metrics at every return point.

        Centralised so the multi-return ``_handle_pr`` doesn't grow a
        bespoke 12-line emission block at each branch. ``"none"`` is the
        sentinel for unset values — both the log and the metrics use it.
        """
        duration_seconds = max(0.0, time.monotonic() - start_monotonic)
        duration_ms = int(duration_seconds * 1000)
        repo_slug = f"{owner}/{repo}"
        tier_label = tier or "none"
        backend_label = backend or "none"
        verdict_label = verdict or "none"
        observe_pr_review_duration(backend=backend_label, tier=tier_label, seconds=duration_seconds)
        record_pr_review_outcome(
            repo=repo_slug, backend=backend_label, tier=tier_label, verdict=verdict_label
        )
        if span is not None:
            try:
                if tier:
                    span.set_attribute("caretaker.complexity.tier", tier_label)
                if backend:
                    span.set_attribute("caretaker.backend", backend_label)
                if model:
                    span.set_attribute("caretaker.review.model", model)
                span.set_attribute("caretaker.review.verdict", verdict_label)
                span.set_attribute("caretaker.auto_fix.dispatched", bool(auto_fix_dispatched))
            except Exception:  # pragma: no cover - defensive
                pass
        logger.info(
            "pr_review_complete repo=%s pr=%d author=%s is_caretaker_owned=%s "
            "routing=%s tier=%s backend=%s model=%s verdict=%s duration_ms=%d "
            "auto_fix_dispatched=%s auto_fix_reason=%s",
            repo_slug,
            pr_number,
            pr_author,
            is_caretaker_pr,
            routing_reason,
            tier_label,
            backend_label,
            model or "none",
            verdict_label,
            duration_ms,
            auto_fix_dispatched,
            auto_fix_reason or "n/a",
            # Every field that appears in the format string above is
            # also surfaced here so structured-log consumers (Loki /
            # JSON formatter) don't lose attributes that the human
            # message includes. Field names match the metric labels
            # where they overlap (``backend``, ``tier``, ``verdict``,
            # ``repo``).
            extra={
                "audit_event": "pr_review_complete",
                "pr_number": pr_number,
                "repo": repo_slug,
                "pr_author": pr_author,
                "is_caretaker_owned": is_caretaker_pr,
                "routing_reason": routing_reason,
                "verdict": verdict_label,
                "tier": tier_label,
                "backend": backend_label,
                "model": model or "none",
                "duration_ms": duration_ms,
                "auto_fix_dispatched": auto_fix_dispatched,
                "auto_fix_reason": auto_fix_reason or "n/a",
            },
        )

    async def _handle_pr(
        self,
        pr: dict[str, Any],
        report: _PRReviewReport,
        *,
        state: OrchestratorState,
    ) -> None:
        # TODO(state-object): the local-variable bag below
        # (``tier_label``, ``backend_label``, ``auto_fix_dispatched`` …)
        # is approaching a state-machine. Refactor into a
        # ``_PRReviewState`` dataclass once the next reviewer phase
        # adds more transitions. Out of scope for phase 1A.
        pr_number = int(pr.get("number", 0))
        owner = self._ctx.owner
        repo = self._ctx.repo

        # Root span for this PR review — every downstream span (complexity
        # classifier, inline reviewer, opencode invoke, auto-fix dispatch)
        # nests under this so a single PR review becomes one trace tree.
        with _tracer.start_as_current_span("pr_reviewer.handle_pr") as span:
            try:
                span.set_attribute("caretaker.pr.repo", f"{owner}/{repo}")
                span.set_attribute("caretaker.pr.number", int(pr_number))
            except Exception:  # pragma: no cover - defensive
                pass
            try:
                await self._handle_pr_body(pr, report, state=state, span=span)
            except Exception as exc:
                try:
                    span.record_exception(exc)
                    span.set_status(
                        _otel_trace.Status(_otel_trace.StatusCode.ERROR, str(exc)[:200])
                    )
                except Exception:  # pragma: no cover
                    pass
                raise

    async def _handle_pr_body(
        self,
        pr: dict[str, Any],
        report: _PRReviewReport,
        *,
        state: OrchestratorState,
        span: Any,
    ) -> None:
        """Real body of :meth:`_handle_pr`, wrapped by the root span."""
        cfg = self._ctx.config.pr_reviewer
        pr_number = int(pr.get("number", 0))
        owner = self._ctx.owner
        repo = self._ctx.repo

        # Track wall-clock from the very top so every early-return branch
        # records the time actually spent before bailing.
        start_monotonic = time.monotonic()
        pr_author = (pr.get("user") or {}).get("login", "")
        is_caretaker_pr = _is_caretaker_owned(pr)
        routing_reason = "n/a"
        tier_label: str | None = None
        backend_label: str | None = None
        model_label: str | None = None
        verdict_label: str | None = None
        auto_fix_dispatched = False
        auto_fix_reason: str | None = None

        try:
            span.set_attribute("caretaker.pr.author", pr_author)
            span.set_attribute("caretaker.pr.is_caretaker_owned", bool(is_caretaker_pr))
        except Exception:  # pragma: no cover - defensive
            pass

        # Skip drafts
        if cfg.skip_draft and pr.get("draft", False):
            report.skipped.append(pr_number)
            self._emit_pr_review_audit(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                pr_author=pr_author,
                is_caretaker_pr=is_caretaker_pr,
                routing_reason="draft",
                tier=tier_label,
                backend=backend_label,
                model=model_label,
                verdict="skipped",
                start_monotonic=start_monotonic,
                auto_fix_dispatched=auto_fix_dispatched,
                auto_fix_reason="skipped_draft",
                span=span,
            )
            return

        # Skip if already reviewed by caretaker this cycle
        pr_labels = [
            lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
            for lbl in pr.get("labels", [])
        ]
        if any(lbl in cfg.skip_labels for lbl in pr_labels):
            report.skipped.append(pr_number)
            self._emit_pr_review_audit(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                pr_author=pr_author,
                is_caretaker_pr=is_caretaker_pr,
                routing_reason="skip_label",
                tier=tier_label,
                backend=backend_label,
                model=model_label,
                verdict="skipped",
                start_monotonic=start_monotonic,
                auto_fix_dispatched=auto_fix_dispatched,
                auto_fix_reason="skipped_label",
                span=span,
            )
            return

        # Harvest pass — if a hand-off agent (Claude Code, opencode)
        # has replied with a ``caretaker-review`` JSON payload, re-post
        # it as a formal PR review so it appears in the Reviews tab
        # under caretaker's bot identity. Runs *before* the routing
        # decision so a freshly-harvested review short-circuits both
        # the inline LLM path and a duplicate hand-off dispatch.
        commit_sha = (pr.get("head") or {}).get("sha", "")
        if commit_sha:
            tracking = state.tracked_prs.get(pr_number) or TrackedPR(number=pr_number)
            posted = await handoff_review_consumer.consume_handoff_reviews(
                github=self._ctx.github,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                head_sha=commit_sha,
                tracking=tracking,
            )
            # Persist tracking back so the consumed-IDs survive the run.
            # This may be the first time pr_reviewer touches this PR's
            # tracking; that's fine — pr_agent and pr_reviewer share the
            # same dict.
            state.tracked_prs[pr_number] = tracking
            if posted:
                report.harvested.append(pr_number)
                # Check if any harvested review requests changes — dispatch auto-fix.
                # We're already inside ``if posted:`` so ``posted[0]`` is safe.
                harvested_verdict = posted[0].verdict
                for review_result in posted:
                    if review_result.verdict == "REQUEST_CHANGES":
                        head_branch = (pr.get("head") or {}).get("ref", "")
                        pr_url = pr.get("html_url", "")
                        fix_decision = _auto_fix.decide_auto_fix(
                            review=review_result,
                            config=cfg.auto_fix,
                            pr_author=pr_author,
                            pr_labels=pr_labels,
                            tracking=tracking,
                            repo=f"{owner}/{repo}",
                        )
                        harvested_verdict = review_result.verdict
                        if fix_decision.should_dispatch:
                            await _auto_fix.dispatch_auto_fix(
                                decision=fix_decision,
                                pr_url=pr_url,
                                head_branch=head_branch,
                                review=review_result,
                                config=cfg,
                                github=self._ctx.github,
                                owner=owner,
                                repo=repo,
                                pr_number=pr_number,
                                tracking=tracking,
                            )
                            auto_fix_dispatched = True
                            auto_fix_reason = fix_decision.reason
                            backend_label = fix_decision.backend
                        else:
                            auto_fix_reason = fix_decision.reason
                        break  # one REQUEST_CHANGES is enough to arm the fixer
                # Always mark reviewed after harvest regardless of fix dispatch.
                try:
                    reviewed_label = "caretaker:reviewed"
                    await self._ctx.github.ensure_label(
                        owner,
                        repo,
                        reviewed_label,
                        color="0075ca",
                        description="Reviewed by caretaker",
                    )
                    await self._ctx.github.add_labels(owner, repo, pr_number, [reviewed_label])
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning(
                        "pr-reviewer: harvested review on #%d but "
                        "failed to apply reviewed label: %s",
                        pr_number,
                        exc,
                    )
                self._emit_pr_review_audit(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    pr_author=pr_author,
                    is_caretaker_pr=is_caretaker_pr,
                    routing_reason="harvest",
                    tier=tier_label,
                    backend=backend_label,
                    model=model_label,
                    verdict=harvested_verdict,
                    start_monotonic=start_monotonic,
                    auto_fix_dispatched=auto_fix_dispatched,
                    auto_fix_reason=auto_fix_reason,
                    span=span,
                )
                return

        # Fetch file metadata for routing
        try:
            files = await self._ctx.github.list_pull_request_files(owner, repo, pr_number)
        except Exception as exc:
            logger.warning("pr-reviewer: cannot fetch files for #%d: %s", pr_number, exc)
            files = []

        additions = sum(int(f.get("additions", 0)) for f in files)
        deletions = sum(int(f.get("deletions", 0)) for f in files)
        file_paths = [f.get("path", "") for f in files]

        decision = decide(
            additions=additions,
            deletions=deletions,
            file_count=len(files),
            file_paths=file_paths,
            pr_labels=pr_labels,
            threshold=cfg.routing_threshold,
            backend=cfg.complex_reviewer,
        )
        routing_reason = decision.reason
        logger.info("pr-reviewer: #%d routing — %s", pr_number, decision.reason)
        try:
            span.set_attribute("caretaker.routing.use_inline", bool(decision.use_inline))
            span.set_attribute("caretaker.routing.score", int(decision.score))
        except Exception:  # pragma: no cover
            pass

        # Shadow-mode wrapper: compares legacy point-system verdict with
        # the LLM candidate under the ``executor_routing`` flag. The
        # return value is the authoritative ``ExecutorRoute`` (legacy in
        # off/shadow, candidate in enforce) but we continue to use the
        # raw ``decision`` object below so inline-path behavior stays
        # byte-identical until enforce mode flips.
        await self._route_via_shadow(
            pr_number=pr_number,
            decision=decision,
            files=files,
            file_paths=file_paths,
            additions=additions,
            deletions=deletions,
            pr_labels=pr_labels,
            pr=pr,
        )

        if decision.use_inline:
            if self._ctx.llm_router is None or not self._ctx.llm_router.available:
                logger.warning(
                    "pr-reviewer: LLM unavailable for inline review of #%d, falling back",
                    pr_number,
                )
                # Fall through to the hand-off path below.
            else:
                from caretaker.llm.claude import StructuredCompleteError

                try:
                    result = await inline_reviewer.review(
                        github=self._ctx.github,
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        pr_title=str(pr.get("title", "")),
                        pr_body=str(pr.get("body") or ""),
                        llm=self._ctx.llm_router,
                        max_diff_lines=cfg.max_diff_lines,
                    )
                except StructuredCompleteError as exc:
                    # LLM returned malformed output after retries — fall through
                    # to the claude-code hand-off path rather than silently
                    # posting an empty COMMENT review (the old behavior).
                    logger.warning(
                        "pr-reviewer: inline review validation failed for #%d: %s — "
                        "falling back to claude-code dispatch",
                        pr_number,
                        exc.validation_error,
                    )
                else:
                    commit_sha = (pr.get("head") or {}).get("sha", "")
                    if not commit_sha:
                        logger.warning("pr-reviewer: no head SHA for #%d", pr_number)
                        report.skipped.append(pr_number)
                        # ``pr_author`` is already set at the top of _handle_pr.
                        self._emit_pr_review_audit(
                            owner=owner,
                            repo=repo,
                            pr_number=pr_number,
                            pr_author=pr_author,
                            is_caretaker_pr=is_caretaker_pr,
                            routing_reason=routing_reason,
                            tier=tier_label,
                            backend="inline",
                            model=model_label,
                            verdict="skipped",
                            start_monotonic=start_monotonic,
                            auto_fix_dispatched=auto_fix_dispatched,
                            auto_fix_reason="no_head_sha",
                            span=span,
                        )
                        return

                    await post_review(
                        github=self._ctx.github,
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        commit_sha=commit_sha,
                        result=result,
                        post_inline_comments=cfg.post_inline_comments,
                        force_event=cfg.review_event if cfg.review_event != "AUTO" else None,
                    )
                    backend_label = "inline"
                    verdict_label = result.verdict
                    # Inline path auto-fix: if the LLM says REQUEST_CHANGES, dispatch fixer.
                    if result.verdict == "REQUEST_CHANGES":
                        _tracking = state.tracked_prs.get(pr_number) or TrackedPR(number=pr_number)
                        head_branch = (pr.get("head") or {}).get("ref", "")
                        pr_url = pr.get("html_url", "")
                        _decision = _auto_fix.decide_auto_fix(
                            review=result,
                            config=cfg.auto_fix,
                            pr_author=pr_author,
                            pr_labels=pr_labels,
                            tracking=_tracking,
                            repo=f"{owner}/{repo}",
                        )
                        auto_fix_reason = _decision.reason
                        if _decision.should_dispatch:
                            await _auto_fix.dispatch_auto_fix(
                                decision=_decision,
                                pr_url=pr_url,
                                head_branch=head_branch,
                                review=result,
                                config=cfg,
                                github=self._ctx.github,
                                owner=owner,
                                repo=repo,
                                pr_number=pr_number,
                                tracking=_tracking,
                            )
                            auto_fix_dispatched = True
                            state.tracked_prs[pr_number] = _tracking
                    # Mark as reviewed
                    try:
                        reviewed_label = "caretaker:reviewed"
                        await self._ctx.github.ensure_label(
                            owner,
                            repo,
                            reviewed_label,
                            color="0075ca",
                            description="Reviewed by caretaker",
                        )
                        await self._ctx.github.add_labels(owner, repo, pr_number, [reviewed_label])
                    except Exception:
                        pass
                    report.reviewed.append(pr_number)
                    self._emit_pr_review_audit(
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        pr_author=pr_author,
                        is_caretaker_pr=is_caretaker_pr,
                        routing_reason=routing_reason,
                        tier=tier_label,
                        backend=backend_label,
                        model=model_label,
                        verdict=verdict_label,
                        start_monotonic=start_monotonic,
                        auto_fix_dispatched=auto_fix_dispatched,
                        auto_fix_reason=auto_fix_reason,
                        span=span,
                    )
                    return

        # Hand-off path — backend chosen by ``complex_reviewer`` (Claude
        # Code, opencode, pr_agent, …). Falls back to opencode if the
        # configured backend isn't recognized or isn't enabled so a
        # misconfiguration doesn't silently skip review entirely.
        backend = decision.backend or cfg.complex_reviewer or "opencode"

        # Caretaker-owned PRs bypass the comment-trigger round-trip: we
        # run opencode (or the configured caretaker_owned_reviewer) as a
        # local subprocess so the review is synchronous, then immediately
        # dispatch auto-fix and auto-approve within the same cycle.
        # ``pr_author`` and ``is_caretaker_pr`` were already populated at
        # the top of _handle_pr; reuse them here.
        if is_caretaker_pr and cfg.caretaker_owned_reviewer:
            caretaker_backend = cfg.caretaker_owned_reviewer
            if caretaker_backend in handoff_reviewer.known_backends():
                caretaker_spec = handoff_reviewer.get_spec(caretaker_backend)
                if caretaker_spec.invocation == "local_subprocess":
                    logger.info(
                        "pr-reviewer: caretaker-owned PR #%d — using %s (local subprocess)",
                        pr_number,
                        caretaker_backend,
                    )
                    backend = caretaker_backend

        if backend not in handoff_reviewer.known_backends():
            logger.warning(
                "pr-reviewer: complex_reviewer=%r is not a known hand-off backend "
                "(known: %s); falling back to opencode",
                backend,
                ", ".join(handoff_reviewer.known_backends()),
            )
            backend = "opencode"
        if cfg.enabled_backends and backend not in cfg.enabled_backends:
            # For caretaker-owned PRs using opencode_local, add it
            # implicitly rather than silently skipping review.
            if is_caretaker_pr and backend == cfg.caretaker_owned_reviewer:
                logger.info(
                    "pr-reviewer: allowing %r for caretaker-owned PR #%d "
                    "(not in enabled_backends but required for caretaker flow)",
                    backend,
                    pr_number,
                )
            else:
                fallback = next(
                    (b for b in cfg.enabled_backends if b in handoff_reviewer.known_backends()),
                    None,
                )
                if fallback is None:
                    logger.error(
                        "pr-reviewer: backend %r not in enabled_backends=%s and no "
                        "known fallback is available — skipping review",
                        backend,
                        cfg.enabled_backends,
                    )
                    self._emit_pr_review_audit(
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        pr_author=pr_author,
                        is_caretaker_pr=is_caretaker_pr,
                        routing_reason=routing_reason,
                        tier=tier_label,
                        backend=backend,
                        model=model_label,
                        verdict="skipped",
                        start_monotonic=start_monotonic,
                        auto_fix_dispatched=auto_fix_dispatched,
                        auto_fix_reason="backend_not_enabled",
                        span=span,
                    )
                    return
                logger.warning(
                    "pr-reviewer: backend %r is registered but not in enabled_backends=%s; "
                    "falling back to %r",
                    backend,
                    cfg.enabled_backends,
                    fallback,
                )
                backend = fallback

        spec = handoff_reviewer.get_spec(backend)
        if spec.invocation == "local_subprocess":
            # Classify complexity once for caretaker-owned PRs so review
            # and any subsequent fix can route to a cheap model when
            # appropriate.  The classifier short-circuits trivial PRs
            # without an LLM call (~30% of bot PRs); for the rest a
            # Flash-Lite call costs ~$0.0001.
            tier = (
                await self._classify_complexity(
                    pr=pr,
                    files=files,
                    pr_labels=pr_labels,
                    routing_decision=decision,
                )
                if is_caretaker_pr
                else None
            )
            tier_label = tier
            backend_label = backend
            model_label = self._resolve_backend_model(backend, tier=tier, mode="review")

            local_result = await self._run_local_subprocess_backend(
                backend=backend,
                spec=spec,
                pr=pr,
                pr_number=pr_number,
                owner=owner,
                repo=repo,
                report=report,
                routing_reason=decision.reason,
                tier=tier,
            )
            if local_result is not None:
                verdict_label = local_result.verdict
            # For caretaker-owned PRs: run auto-fix when reviewer says
            # REQUEST_CHANGES, then auto-approve on success.
            if (
                local_result is not None
                and local_result.verdict == "REQUEST_CHANGES"
                and is_caretaker_pr
            ):
                head_branch = (pr.get("head") or {}).get("ref", "")
                pr_url = pr.get("html_url", "") or (
                    f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                )
                _tracking = state.tracked_prs.get(pr_number) or TrackedPR(number=pr_number)
                fix_decision = _auto_fix.decide_auto_fix(
                    review=local_result,
                    config=cfg.auto_fix,
                    pr_author=pr_author,
                    pr_labels=pr_labels,
                    tracking=_tracking,
                    repo=f"{owner}/{repo}",
                )
                auto_fix_reason = fix_decision.reason
                if fix_decision.should_dispatch:
                    outcome = await _auto_fix.dispatch_auto_fix(
                        decision=fix_decision,
                        pr_url=pr_url,
                        head_branch=head_branch,
                        review=local_result,
                        config=cfg,
                        github=self._ctx.github,
                        owner=owner,
                        repo=repo,
                        pr_number=pr_number,
                        tracking=_tracking,
                        tier=tier,
                    )
                    auto_fix_dispatched = True
                    state.tracked_prs[pr_number] = _tracking
                    if outcome.success and outcome.new_head_sha:
                        await self._auto_approve_caretaker_pr(
                            pr_number=pr_number,
                            owner=owner,
                            repo=repo,
                            new_head_sha=outcome.new_head_sha,
                            fix_backend=fix_decision.backend,
                        )
            self._emit_pr_review_audit(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                pr_author=pr_author,
                is_caretaker_pr=is_caretaker_pr,
                routing_reason=routing_reason,
                tier=tier_label,
                backend=backend_label,
                model=model_label,
                verdict=verdict_label,
                start_monotonic=start_monotonic,
                auto_fix_dispatched=auto_fix_dispatched,
                auto_fix_reason=auto_fix_reason,
                span=span,
            )
            return

        backend_label = backend
        success = await handoff_reviewer.dispatch(
            backend=backend,
            github=self._ctx.github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            config=cfg,
            routing_reason=decision.reason,
        )
        if success:
            report.dispatched.append(pr_number)
            verdict_label = "dispatched"
        else:
            report.errors.append(f"{backend} dispatch failed for #{pr_number}")
            verdict_label = "dispatch_failed"
        self._emit_pr_review_audit(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_author=pr_author,
            is_caretaker_pr=is_caretaker_pr,
            routing_reason=routing_reason,
            tier=tier_label,
            backend=backend_label,
            model=model_label,
            verdict=verdict_label,
            start_monotonic=start_monotonic,
            auto_fix_dispatched=auto_fix_dispatched,
            auto_fix_reason=auto_fix_reason,
            span=span,
        )

    async def _run_local_subprocess_backend(
        self,
        *,
        backend: str,
        spec: handoff_reviewer.HandoffReviewerSpec,
        pr: dict[str, Any],
        pr_number: int,
        owner: str,
        repo: str,
        report: _PRReviewReport,
        routing_reason: str,
        tier: ComplexityTier | None = None,
    ) -> ReviewResult | None:
        """Run a local-subprocess backend (pr_agent, greptile) and post the review.

        Unlike the comment-trigger path, the review is produced
        synchronously by caretaker itself, so we post the formal Reviews
        API entry directly rather than waiting for a cross-cycle harvest.
        Dispatch failures fall back to a regular comment so the operator
        sees what went wrong without losing the PR's review slot.
        """
        cfg = self._ctx.config.pr_reviewer
        commit_sha = (pr.get("head") or {}).get("sha", "")
        if not commit_sha:
            logger.warning(
                "pr-reviewer(%s): no head SHA on #%d; cannot post review", backend, pr_number
            )
            report.skipped.append(pr_number)
            return None

        if spec.runner is None:
            logger.error(
                "pr-reviewer(%s): spec marked local_subprocess but runner is None; skipping #%d",
                backend,
                pr_number,
            )
            report.errors.append(f"{backend} runner missing for #{pr_number}")
            return None

        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
        backend_config = self._resolve_local_backend_config(backend)

        # Only opencode_local currently knows how to use ``tier``; other
        # local-subprocess backends keep the legacy two-arg signature so
        # we don't fail with TypeError on unexpected kwargs.
        runner_kwargs: dict[str, Any] = {"pr_url": pr_url, "config": backend_config}
        if tier is not None and backend == "opencode_local":
            runner_kwargs["tier"] = tier

        try:
            review_result = await spec.runner(**runner_kwargs)
        except Exception as exc:  # noqa: BLE001 — we always want to log + fall back
            logger.warning(
                "pr-reviewer(%s): runner failed on %s/%s#%d: %s",
                backend,
                owner,
                repo,
                pr_number,
                exc,
            )
            try:
                await self._ctx.github.upsert_issue_comment(
                    owner,
                    repo,
                    pr_number,
                    marker=spec.marker,
                    body=(
                        f"{spec.marker}\n\n"
                        f"caretaker tried to run the **{backend}** review backend "
                        f"and it failed: `{exc}`. The PR is unreviewed by this backend; "
                        "consider re-running or switching `pr_reviewer.complex_reviewer`."
                    ),
                )
            except Exception as post_exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "pr-reviewer(%s): also failed to post failure note: %s", backend, post_exc
                )
            report.errors.append(f"{backend} runner failed for #{pr_number}: {exc}")
            return None

        try:
            await post_review(
                github=self._ctx.github,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                commit_sha=commit_sha,
                result=review_result,
                post_inline_comments=cfg.post_inline_comments,
                force_event=cfg.review_event if cfg.review_event != "AUTO" else None,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "pr-reviewer(%s): post_review failed on %s/%s#%d: %s",
                backend,
                owner,
                repo,
                pr_number,
                exc,
            )
            report.errors.append(f"{backend} post_review failed for #{pr_number}: {exc}")
            return None

        try:
            reviewed_label = "caretaker:reviewed"
            await self._ctx.github.ensure_label(
                owner, repo, reviewed_label, color="0075ca", description="Reviewed by caretaker"
            )
            await self._ctx.github.add_labels(owner, repo, pr_number, [reviewed_label])
        except Exception as exc:  # noqa: BLE001 — best-effort, don't drop the review
            logger.warning(
                "pr-reviewer(%s): posted review on #%d but failed to apply reviewed label: %s",
                backend,
                pr_number,
                exc,
            )
        report.reviewed.append(pr_number)
        return review_result

    async def _classify_complexity(
        self,
        *,
        pr: dict[str, Any],
        files: list[dict[str, Any]],
        pr_labels: list[str],
        routing_decision: Any,
    ) -> ComplexityTier | None:
        """Classify a caretaker-owned PR's complexity for tier-based model selection.

        Returns the tier string (``trivial``/``simple``/``standard``/``complex``)
        or ``None`` on any failure — callers fall back to the configured
        default model when ``None`` is returned.

        Builds an :class:`ExecutorRouteContext` from the same signals the
        existing routing pass already gathered (file list, LOC, labels)
        so we don't re-fetch GitHub data.
        """
        from caretaker.evolution.executor_routing import (  # noqa: PLC0415
            ExecutorRouteContext,
            ExecutorRouteFile,
        )
        from caretaker.pr_reviewer import complexity_classifier  # noqa: PLC0415

        try:
            route_files = [
                ExecutorRouteFile(
                    path=f.get("path", ""),
                    additions=int(f.get("additions", 0)),
                    deletions=int(f.get("deletions", 0)),
                )
                for f in files
            ]
            context = ExecutorRouteContext(
                task_type="pr_review",
                files=route_files,
                labels=pr_labels,
                repo_slug=f"{self._ctx.owner}/{self._ctx.repo}",
                title=str(pr.get("title", "")),
                body=str(pr.get("body") or ""),
            )
            claude = self._ctx.llm_router.claude if self._ctx.llm_router else None
            verdict = await complexity_classifier.classify(
                context=context,
                claude=claude,
                routing_decision=routing_decision,
            )
            logger.info(
                "pr-reviewer: complexity tier=%s (confidence=%.2f) — %s",
                verdict.tier,
                verdict.confidence,
                verdict.reason,
            )
            return verdict.tier
        except Exception as exc:  # noqa: BLE001 — never fail the review on classifier errors
            logger.warning(
                "pr-reviewer: complexity classification failed (%s); using default models",
                exc,
            )
            return None

    async def _auto_approve_caretaker_pr(
        self,
        *,
        pr_number: int,
        owner: str,
        repo: str,
        new_head_sha: str,
        fix_backend: str,
    ) -> None:
        """Submit an APPROVE review after a successful auto-fix on a caretaker-owned PR.

        Posts a formal GitHub PR approval attributed to caretaker's bot
        identity, marking the review feedback as addressed.  Any error
        is logged but not propagated — the fix commit has already been
        pushed and is more important than the approval label.
        """
        body = (
            "caretaker auto-fix applied successfully. "
            f"Feedback addressed by `{fix_backend}` "
            f"(commit `{new_head_sha[:8]}`). Approving."
        )
        try:
            await self._ctx.github.create_review(
                owner,
                repo,
                pr_number,
                commit_sha=new_head_sha,
                body=body,
                event="APPROVE",
            )
            logger.info(
                "pr-reviewer: auto-approved caretaker PR #%d after %s fix (%s)",
                pr_number,
                fix_backend,
                new_head_sha[:8],
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "pr-reviewer: failed to auto-approve caretaker PR #%d: %s",
                pr_number,
                exc,
            )

    def _resolve_local_backend_config(self, backend: str) -> Any:
        """Return the per-backend config object passed to the runner.

        Convention: a backend named ``foo_bar`` looks up
        ``PRReviewerConfig.foo_bar``. Stub backends without a config
        block get an empty namespace so their runner can raise
        ``NotImplementedError`` cleanly without a missing-attribute.
        """
        return getattr(self._ctx.config.pr_reviewer, backend, _EMPTY_BACKEND_CONFIG)

    def _resolve_backend_model(
        self,
        backend: str,
        *,
        tier: ComplexityTier | None,
        mode: Literal["review", "fix"],
    ) -> str | None:
        """Return the model id the backend would use for ``mode``+``tier``.

        Best-effort lookup so the audit log reports the actual model the
        backend will route to (rather than the operator-pinned default
        even when a tier override is in play). Returns ``None`` when the
        backend doesn't expose a tier-aware model map.

        TODO(backend-dispatch): replace the ``if backend == ...``
        ladder with a dispatch table once a second tier-aware backend
        lands. Out of scope for the phase 1A observability PR.
        """
        backend_cfg = self._resolve_local_backend_config(backend)
        if backend == "opencode_local":
            tier_map = getattr(
                backend_cfg, "review_models" if mode == "review" else "fix_models", {}
            )
            default = (
                getattr(backend_cfg, "model", "")
                if mode == "review"
                else (getattr(backend_cfg, "fix_model", "") or getattr(backend_cfg, "model", ""))
            )
            if tier is not None and tier_map.get(tier):
                model: str = tier_map[tier]
                return model
            return str(default) if default else None
        configured: Any = getattr(backend_cfg, "model", None)
        return str(configured) if configured else None

    async def _route_via_shadow(
        self,
        *,
        pr_number: int,
        decision: Any,
        files: list[dict[str, Any]],
        file_paths: list[str],
        additions: int,
        deletions: int,
        pr_labels: list[str],
        pr: dict[str, Any],
    ) -> ExecutorRoute | None:
        """Run the ``executor_routing`` shadow gate for a PR-reviewer decision.

        Always returns the legacy-adapted :class:`ExecutorRoute` in
        ``off`` / ``shadow`` modes; returns the LLM verdict when
        ``enforce`` mode succeeds. Defensive: any exception bubbling out
        of the decorator is swallowed and ``None`` is returned so the
        caller continues to use the point-system ``decision`` object.
        """

        async def _legacy_path() -> ExecutorRoute:
            return route_from_pr_reviewer_legacy(
                decision,
                additions=additions,
                deletions=deletions,
                file_count=len(files),
                file_paths=file_paths,
            )

        async def _candidate_path() -> ExecutorRoute | None:
            if self._ctx.llm_router is None or not self._ctx.llm_router.available:
                return None
            route_files = [
                ExecutorRouteFile(
                    path=f.get("path", ""),
                    additions=int(f.get("additions", 0)),
                    deletions=int(f.get("deletions", 0)),
                )
                for f in files
            ]
            context = ExecutorRouteContext(
                task_type="pr_review",
                files=route_files,
                labels=pr_labels,
                repo_slug=f"{self._ctx.owner}/{self._ctx.repo}",
                candidate_paths=["inline", "opencode"],
                title=str(pr.get("title", "")),
                body=str(pr.get("body") or ""),
            )
            return await route_executor_llm(context, claude=self._ctx.llm_router.claude)

        try:
            return await _decide_executor_route(
                legacy=_legacy_path,
                candidate=_candidate_path,
                context={
                    "pr_number": pr_number,
                    "repo_slug": f"{self._ctx.owner}/{self._ctx.repo}",
                    "site": "pr_reviewer",
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive: never fail the agent
            logger.warning(
                "pr-reviewer: executor_routing shadow-decision failed for #%d (%s: %s)",
                pr_number,
                type(exc).__name__,
                exc,
            )
            return None
