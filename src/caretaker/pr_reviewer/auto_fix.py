"""Auto-fix loop: dispatch a fixer when the reviewer says REQUEST_CHANGES.

Sits downstream of the reviewer in :class:`PRReviewerAgent`. Eligibility
gates first (label opt-in / bot author / iteration cap), then routes by
``ReviewResult.issue_categories`` into one of:

  * ``deterministic_lint`` — runs ``ruff format && ruff check --fix``
    in the cloned workdir, commits, pushes. No LLM, no cost, zero
    trust risk. The default for ``lint``/``format`` categories.
  * Any registered backend with ``invocation == "local_subprocess"`` —
    runs that backend's runner with ``task="fix"`` so the runner can
    branch (e.g. claude_code_local switches to a fix-mode prompt + the
    ``acceptEdits`` permission so it actually writes files).

The dispatcher pushes the resulting commits to the PR's head branch,
which triggers another review on caretaker's next cycle (or via webhook
if wired). The loop is bounded by ``AutoFixConfig.max_attempts`` per PR
and the per-PR counter on :class:`TrackedPR` resets when the head SHA
changes outside the loop (a human edit re-arms the loop).

Decoupling reviewer from fixer matters: same-lineage models tend to
validate each other's mistakes ("trust spiral"). The ``category_to_fixer``
map lets you wire reviewer=claude → fixer=opencode (or
deterministic_lint for mechanical issues) so the validator and the
modified are different.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from opentelemetry import trace as _otel_trace

from caretaker.observability.metrics import record_auto_fix_dispatch
from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output

if TYPE_CHECKING:
    from caretaker.config import AutoFixConfig, PRReviewerConfig
    from caretaker.github_client.api import GitHubClient
    from caretaker.pr_reviewer.inline_reviewer import ReviewResult
    from caretaker.state.models import TrackedPR

logger = logging.getLogger(__name__)

# Module-level tracer — every dispatch becomes one span under the parent
# ``pr_reviewer.handle_pr`` trace, carrying backend + categories + outcome.
_tracer = _otel_trace.get_tracer("caretaker.pr_reviewer.auto_fix")


# Synthetic backend name handled in-process by :func:`run_deterministic_lint`
# rather than dispatched to a HandoffReviewerSpec runner. Listed here so
# the dispatcher can recognise it without importing config defaults.
DETERMINISTIC_LINT_BACKEND = "deterministic_lint"


# Heuristic keyword classifier — used when the reviewer didn't fill
# ``issue_categories`` (older agent reply, free-form prose, etc.). Order
# matters: most specific keywords first so "lint" doesn't swallow
# "linter-style security check". Each pattern is matched
# case-insensitively against the review summary + per-comment bodies.
_HEURISTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("security", re.compile(r"(?<!\bno\s)(security|vulnerab|injection|xss|csrf|secret|cve)", re.I)),
    ("test", re.compile(r"(test coverage|missing test|untested|test fail|broken test)", re.I)),
    (
        "type",
        re.compile(
            r"(type[- ]?(check|annotation|hint|error)|mypy|pyright|incompatible types)", re.I
        ),
    ),
    (
        "format",
        re.compile(r"(format(ting)?|style guide|whitespace|trailing comma|prettier|black)", re.I),
    ),
    (
        "lint",
        re.compile(r"(lint(er|ing)?|ruff|eslint|flake8|pylint|unused (import|variable))", re.I),
    ),
    ("docs", re.compile(r"(docstring|missing doc|update docs?|README)", re.I)),
    (
        "correctness",
        re.compile(
            r"(bug|incorrect|wrong|broken|race condition|off[- ]by[- ]one|null pointer)", re.I
        ),
    ),
)


def classify_issue_categories(result: ReviewResult) -> list[str]:
    """Return the categories implied by a ``ReviewResult``.

    Reviewer-supplied categories win. When the field is empty, run the
    keyword heuristic over summary + comment bodies and return the
    matches in pattern order (most-specific first). Returns an empty
    list when nothing matches; the caller falls back to
    ``AutoFixConfig.default_fixer``.
    """
    if result.issue_categories:
        return list(result.issue_categories)
    haystack = result.summary + "\n" + "\n".join(c.body for c in result.comments)
    matches: list[str] = []
    for category, pattern in _HEURISTIC_PATTERNS:
        if pattern.search(haystack):
            matches.append(category)
    return matches


@dataclass(frozen=True)
class AutoFixDecision:
    """The dispatcher's verdict for one PR — either skip or run a fixer."""

    should_dispatch: bool
    backend: str = ""  # "deterministic_lint" | a registered local-subprocess backend
    reason: str = ""
    categories: list[str] | None = None


def decide_auto_fix(
    *,
    review: ReviewResult,
    config: AutoFixConfig,
    pr_author: str,
    pr_labels: list[str],
    tracking: TrackedPR,
    repo: str = "",
) -> AutoFixDecision:
    """Return whether to dispatch a fixer for this review, and which one.

    Eligibility short-circuits in this order so misconfiguration / bot
    storms / runaway loops can't bypass any gate:

      1. ``config.enabled`` — global kill switch
      2. ``review.verdict != "REQUEST_CHANGES"`` — nothing to fix
      3. ``tracking.auto_fix_attempts >= max_attempts`` — bound the loop
      4. Author / label opt-in — bot authors auto-eligible; humans need
         the opt-in label
      5. Pick a backend from category → fixer map; fall back to
         ``default_fixer`` when no category resolves cleanly

    ``repo`` is the ``owner/repo`` slug, threaded through so the
    ``skipped`` outcome counter is attributed correctly. Defaults to
    empty for backward-compat with older callers.
    """
    if not config.enabled:
        record_auto_fix_dispatch(repo=repo, backend="none", category="none", outcome="skipped")
        return AutoFixDecision(should_dispatch=False, reason="auto_fix.enabled is False")
    if review.verdict != "REQUEST_CHANGES":
        record_auto_fix_dispatch(repo=repo, backend="none", category="none", outcome="skipped")
        return AutoFixDecision(
            should_dispatch=False, reason=f"verdict {review.verdict!r} is not REQUEST_CHANGES"
        )
    if tracking.auto_fix_attempts >= config.max_attempts:
        record_auto_fix_dispatch(repo=repo, backend="none", category="none", outcome="skipped")
        return AutoFixDecision(
            should_dispatch=False,
            reason=(
                f"max_attempts={config.max_attempts} reached; "
                "escalate manually or force-push to re-arm"
            ),
        )

    author_eligible = pr_author in set(config.allowed_authors)
    label_eligible = config.opt_in_label in pr_labels
    if not (author_eligible or label_eligible):
        record_auto_fix_dispatch(repo=repo, backend="none", category="none", outcome="skipped")
        return AutoFixDecision(
            should_dispatch=False,
            reason=(
                f"author {pr_author!r} not in allowed_authors and "
                f"label {config.opt_in_label!r} absent"
            ),
        )

    categories = classify_issue_categories(review)
    if config.always_run_heuristic and categories:
        # Merge LLM categories with heuristic, preserving order.
        seen = set(categories)
        for c in classify_issue_categories(
            type(review)(summary=review.summary, verdict=review.verdict, comments=review.comments)
        ):
            if c not in seen:
                categories.append(c)
                seen.add(c)

    backend = ""
    for cat in categories:
        mapped = config.category_to_fixer.get(cat)
        if mapped:
            backend = mapped
            break
    if not backend:
        backend = config.default_fixer

    return AutoFixDecision(
        should_dispatch=True,
        backend=backend,
        reason=(
            f"verdict=REQUEST_CHANGES, "
            f"categories={categories or ['<none>']}, "
            f"attempt={tracking.auto_fix_attempts + 1}/{config.max_attempts}"
        ),
        categories=categories,
    )


async def run_deterministic_lint(
    *,
    workdir: str,
    config: AutoFixConfig,
) -> bool:
    """Run the configured lint commands in ``workdir``. Return True if any change resulted.

    Each command is run in sequence; failures are logged but don't abort
    the chain (ruff format may exit non-zero when nothing to format,
    depending on version). The caller checks ``git diff --quiet`` to
    decide whether to commit.
    """
    for cmd in config.deterministic_lint_commands:
        if not cmd:
            continue
        logger.info("auto_fix(lint): running %s in %s", " ".join(cmd), workdir)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await stream_subprocess_output(
                proc,
                timeout_seconds=120,
                stdout_log=lambda line, c=cmd[0]: logger.info("%s | %s", c, line),  # type: ignore[misc]
                stderr_log=lambda line, c=cmd[0]: logger.info("%s! %s", c, line),  # type: ignore[misc]
            )
        except TimeoutError:
            logger.warning("auto_fix(lint): %s timed out", " ".join(cmd))
            continue
        if proc.returncode not in (0, 1):
            # Most linters use 1 to signal "found issues" which is fine
            # because we passed --fix. Anything else is a real error.
            logger.warning(
                "auto_fix(lint): %s exited %d (continuing)", " ".join(cmd), proc.returncode
            )
    # Tell the caller whether there's anything to commit.
    diff_proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--quiet",
        cwd=workdir,
    )
    await diff_proc.wait()
    return diff_proc.returncode != 0  # nonzero = there are unstaged changes


async def commit_and_push(
    *,
    workdir: str,
    branch: str,
    commit_message: str,
    github_token: str,
    owner: str,
    repo: str,
) -> str:
    """Stage, commit, and push the workdir's changes back to the PR branch.

    Returns the new HEAD SHA. The push uses HTTPS-with-token rather than
    relying on whatever remote the clone configured, so a token rotation
    in caretaker's env doesn't strand the loop on stale credentials.
    """
    # Configure identity for this commit only (don't pollute global git config).
    for k, v in (
        ("user.name", "the-care-taker[bot]"),
        ("user.email", "the-care-taker[bot]@users.noreply.github.com"),
    ):
        proc = await asyncio.create_subprocess_exec("git", "config", k, v, cwd=workdir)
        await proc.wait()

    proc = await asyncio.create_subprocess_exec("git", "add", "-A", cwd=workdir)
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("git add -A failed")

    proc = await asyncio.create_subprocess_exec("git", "commit", "-m", commit_message, cwd=workdir)
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("git commit failed (nothing to commit?)")

    push_url = f"https://x-access-token:{github_token}@github.com/{owner}/{repo}.git"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "push",
        push_url,
        f"HEAD:{branch}",
        cwd=workdir,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"git push to {branch} failed")

    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD", cwd=workdir, stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def post_dispatch_comment(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    decision: AutoFixDecision,
    new_head_sha: str,
    success: bool,
    detail: str = "",
) -> None:
    """Drop a status comment on the PR so reviewers see what happened."""
    marker = "<!-- caretaker:auto-fix-status -->"
    icon = "✅" if success else "⚠️"
    body = (
        f"{marker}\n\n"
        f"### {icon} Auto-fix dispatch\n\n"
        f"- **Backend:** `{decision.backend}`\n"
        f"- **Categories:** {', '.join(decision.categories or []) or '_unclassified_'}\n"
        f"- **Reason:** {decision.reason}\n"
        f"- **Result:** {'pushed `' + new_head_sha[:8] + '`' if new_head_sha else 'no commits'}\n"
    )
    if detail:
        body += f"\n<details><summary>Detail</summary>\n\n{detail}\n\n</details>\n"
    try:
        await github.upsert_issue_comment(owner, repo, pr_number, marker=marker, body=body)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("auto_fix: failed to post dispatch status comment: %s", exc)


@dataclass
class AutoFixOutcome:
    """Result of one auto-fix dispatch cycle."""

    dispatched: bool
    success: bool = False
    new_head_sha: str = ""
    detail: str = ""
    error: str = ""


async def dispatch_auto_fix(
    *,
    decision: AutoFixDecision,
    pr_url: str,
    head_branch: str,
    review: ReviewResult,
    config: PRReviewerConfig,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    tracking: TrackedPR,
    tier: str | None = None,
) -> AutoFixOutcome:
    """Execute the chosen fixer, push commits, update tracking + post status.

    Returns an :class:`AutoFixOutcome` describing what happened. The
    caller (PRReviewerAgent) should:
      * On ``dispatched=True, success=True`` — record the new head SHA
        on tracking and treat the next caretaker cycle as a re-review.
      * On ``dispatched=True, success=False`` — log the error and let
        the loop counter prevent further attempts; status comment
        already posted by this function.
      * On ``dispatched=False`` — :func:`decide_auto_fix` already
        explained why; nothing else to do.

    The loop counter is incremented BEFORE the fixer runs so a crash
    mid-fix still costs an attempt — otherwise an adversarial fixer
    that always crashes would never bound the loop.
    """
    auto_fix_cfg = config.auto_fix
    tracking.auto_fix_attempts += 1
    tracking.auto_fix_last_head_sha = ""  # will be set on success below

    span_cm = _tracer.start_as_current_span("auto_fix.dispatch")
    span = span_cm.__enter__()
    try:
        try:
            span.set_attribute("caretaker.pr.repo", f"{owner}/{repo}")
            span.set_attribute("caretaker.pr.number", int(pr_number))
            span.set_attribute("caretaker.auto_fix.backend", str(decision.backend))
            span.set_attribute("caretaker.auto_fix.categories", list(decision.categories or []))
            span.set_attribute("caretaker.auto_fix.attempt", int(tracking.auto_fix_attempts))
        except Exception:  # pragma: no cover - defensive
            pass
        outcome_result = await _dispatch_auto_fix_inner(
            decision=decision,
            pr_url=pr_url,
            head_branch=head_branch,
            review=review,
            config=config,
            github=github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            tracking=tracking,
            tier=tier,
            auto_fix_cfg=auto_fix_cfg,
        )
        try:
            outcome_label = (
                "success"
                if outcome_result.success
                else ("skipped" if not outcome_result.dispatched else "failed")
            )
            span.set_attribute("caretaker.auto_fix.outcome", outcome_label)
            if outcome_result.new_head_sha:
                span.set_attribute("caretaker.auto_fix.new_head_sha", outcome_result.new_head_sha)
        except Exception:  # pragma: no cover
            pass
        return outcome_result
    except Exception as exc:
        try:
            span.record_exception(exc)
            span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR, str(exc)[:200]))
        except Exception:  # pragma: no cover
            pass
        raise
    finally:
        span_cm.__exit__(None, None, None)


async def _dispatch_auto_fix_inner(
    *,
    decision: AutoFixDecision,
    pr_url: str,
    head_branch: str,
    review: ReviewResult,
    config: PRReviewerConfig,
    github: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    tracking: TrackedPR,
    tier: str | None,
    auto_fix_cfg: AutoFixConfig,
) -> AutoFixOutcome:
    """Inner body of :func:`dispatch_auto_fix`, wrapped by the OTel span."""
    workdir: str | None = None
    new_head: str = ""
    try:
        # Lazy imports — avoid pulling backend modules into this file
        # at import time (and the resulting transitive load of every
        # backend's dependencies for callers that just want the
        # decision logic).
        # Pull GITHUB_TOKEN from env (caretaker process environment).
        # The auto-fix flow needs push access, so the token must be
        # present; if absent, fail fast with a clear error.
        import os as _os  # noqa: PLC0415 — local to keep top-of-module clean

        from caretaker.pr_reviewer.backends._workdir import (
            WorkdirError,
            prepare_workdir,
        )

        token = _os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise WorkdirError(
                "GITHUB_TOKEN not set in caretaker's environment; cannot push fix commits"
            )

        workdir, _parsed = await prepare_workdir(
            pr_url,
            clone_depth=50,
            head_branch=head_branch,
            github_token=token,
        )

        if decision.backend == DETERMINISTIC_LINT_BACKEND:
            changed = await run_deterministic_lint(workdir=workdir, config=auto_fix_cfg)
            if not changed:
                outcome = AutoFixOutcome(
                    dispatched=True,
                    success=False,
                    detail="deterministic_lint produced no changes; nothing to commit",
                )
                await post_dispatch_comment(
                    github=github,
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    decision=decision,
                    new_head_sha="",
                    success=False,
                    detail=outcome.detail,
                )
                record_auto_fix_dispatch(
                    repo=f"{owner}/{repo}",
                    backend=decision.backend,
                    category=(decision.categories or ["none"])[0] or "none",
                    outcome="dispatched_fail",
                )
                return outcome
        else:
            # Look up the backend's fix_run via its module. Only
            # backends that explicitly export ``fix_run`` are valid
            # fixers — anything else is a config error and we surface
            # it so the operator knows to fix the mapping.
            from caretaker.pr_reviewer.handoff_reviewer import get_spec  # noqa: PLC0415

            try:
                get_spec(decision.backend)
            except ValueError as exc:
                raise WorkdirError(f"unknown fixer backend {decision.backend!r}: {exc}") from exc

            backend_module = _resolve_backend_module(decision.backend)
            fix_callable = getattr(backend_module, "fix_run", None)
            if fix_callable is None:
                raise WorkdirError(
                    f"backend {decision.backend!r} module has no fix_run(); "
                    "implement it or remap the category to a backend that does"
                )

            backend_config = getattr(config, decision.backend, None)
            # Only opencode_local currently supports tier-based model
            # selection; pass ``tier`` only there to keep older
            # backends' two-arg signatures intact.
            fix_kwargs: dict[str, object] = {
                "workdir": workdir,
                "review_summary": review.summary,
                "review_comments": review.comments,
                "config": backend_config,
            }
            if tier is not None and decision.backend == "opencode_local":
                fix_kwargs["tier"] = tier
            summary = await fix_callable(**fix_kwargs)
            logger.info("auto_fix(%s): fixer returned: %s", decision.backend, summary[:200])

        # Whether we ran lint or an LLM fixer, we now check for + push
        # the diff. Sharing this step means a future "deterministic
        # type-fixer" backend (e.g. ``mypy --install-types``) plugs in
        # the same way.
        new_head = await commit_and_push(
            workdir=workdir,
            branch=head_branch,
            commit_message=(
                f"{auto_fix_cfg.fix_commit_message} "
                f"[{decision.backend}, attempt {tracking.auto_fix_attempts}]"
            ),
            github_token=token,
            owner=owner,
            repo=repo,
        )
        tracking.auto_fix_last_head_sha = new_head
        outcome = AutoFixOutcome(dispatched=True, success=True, new_head_sha=new_head, detail="")
        await post_dispatch_comment(
            github=github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            decision=decision,
            new_head_sha=new_head,
            success=True,
        )
        record_auto_fix_dispatch(
            repo=f"{owner}/{repo}",
            backend=decision.backend,
            category=(decision.categories or ["none"])[0] or "none",
            outcome="dispatched_success",
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 — the dispatcher is best-effort
        logger.warning(
            "auto_fix: dispatch failed for #%d via %s: %s",
            pr_number,
            decision.backend,
            exc,
        )
        outcome = AutoFixOutcome(
            dispatched=True,
            success=False,
            new_head_sha=new_head,
            error=str(exc),
        )
        with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort
            await post_dispatch_comment(
                github=github,
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                decision=decision,
                new_head_sha=new_head,
                success=False,
                detail=f"`{type(exc).__name__}: {exc}`",
            )
        record_auto_fix_dispatch(
            repo=f"{owner}/{repo}",
            backend=decision.backend,
            category=(decision.categories or ["none"])[0] or "none",
            outcome="dispatched_fail",
        )
        return outcome
    finally:
        if workdir is not None:
            with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort cleanup
                from caretaker.pr_reviewer.backends._workdir import (
                    cleanup_workdir as _cleanup,  # noqa: PLC0415
                )

                _cleanup(workdir, keep=False)


def _resolve_backend_module(backend: str):  # type: ignore[no-untyped-def]
    """Map a backend name to its module so we can look up ``fix_run``.

    Kept here (not in the spec registry) so the spec registry stays
    typed as a dict of ``HandoffReviewerSpec`` — adding ``fix_run`` to
    the spec dataclass would force every backend to provide it,
    including stubs that don't make sense as fixers.

    Raises ``WorkdirError`` for any backend not yet wired into this
    resolver so the error message is actionable ("implement fix_run or
    remap the category") rather than a generic AttributeError on None.
    """
    if backend == "claude_code_local":
        from caretaker.pr_reviewer.backends import claude_code_local  # noqa: PLC0415

        return claude_code_local
    if backend == "opencode_local":
        from caretaker.pr_reviewer.backends import opencode_local  # noqa: PLC0415

        return opencode_local
    from caretaker.pr_reviewer.backends._workdir import WorkdirError  # noqa: PLC0415

    raise WorkdirError(
        f"backend {backend!r} is not registered in _resolve_backend_module; "
        "to use it as a fixer, add an entry here that returns the module "
        "exposing fix_run(), or remap the category to an existing fixer backend"
    )


__all__ = [
    "DETERMINISTIC_LINT_BACKEND",
    "AutoFixDecision",
    "AutoFixOutcome",
    "classify_issue_categories",
    "commit_and_push",
    "decide_auto_fix",
    "dispatch_auto_fix",
    "post_dispatch_comment",
    "run_deterministic_lint",
]
