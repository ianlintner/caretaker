"""``opencode_local`` backend — runs the opencode CLI in caretaker's pod.

An alternative to the ``opencode`` comment-trigger backend (which posts
``@opencode-agent`` and waits for ``sst/opencode/github`` to reply across
cycles). This backend instead:

  1. Clones the PR's head into a temp workdir inside caretaker's pod
  2. Spawns ``opencode run "<review prompt>" --model <id>`` (non-interactive)
  3. Streams every line of output to caretaker's logger (visible live in
     GH Actions / kubectl logs)
  4. Parses the final stdout for a ``caretaker-review`` JSON block,
     transforms it into a :class:`ReviewResult`
  5. Cleans up the workdir (kept on disk only when the run failed AND
     the operator opted into ``keep_workdir_on_failure``)

Compared to the action-based ``opencode`` path it: removes the per-target-
repo workflow install requirement, centralises credentials and observability
in caretaker, and gives synchronous logs — no waiting for the next
orchestrator cycle.

For multi-tenant or high-PR-rate fleets, swap the in-pod subprocess for
a Kubernetes Job per review so each session has its own resource budget.
The runner shape (:func:`run`) is the same; only :func:`_invoke_opencode`
would change.

opencode CLI non-interactive mode:
  ``opencode run "prompt" --model openrouter/<provider>/<model>``
  runs the agent against a prompt and prints the response to stdout.
  ``OPENROUTER_API_KEY`` is inherited from the pod environment; no extra
  configuration needed.  Configure via :class:`OpenCodeLocalBackendConfig`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from typing import TYPE_CHECKING

from opentelemetry import trace as _otel_trace

from caretaker.observability.metrics import record_llm_usage, record_opencode_invocation
from caretaker.pr_reviewer.backends._subprocess_streaming import stream_subprocess_output
from caretaker.pr_reviewer.backends._workdir import (
    WorkdirError,
    cleanup_workdir,
    parse_pr_url,
    prepare_workdir,
)
from caretaker.pr_reviewer.handoff_reviewer import (
    OPENCODE_LOCAL_REVIEW_MARKER,
    HandoffReviewerSpec,
)
from caretaker.pr_reviewer.inline_reviewer import InlineReviewComment, ReviewResult

if TYPE_CHECKING:
    from caretaker.config import OpenCodeLocalBackendConfig
    from caretaker.pr_reviewer.complexity_classifier import ComplexityTier

logger = logging.getLogger(__name__)

# Module-level tracer — wraps each opencode subprocess invocation so the
# parent ``pr_reviewer.handle_pr`` trace shows model + exit_code +
# outcome. Note: the ``parse_fallback`` outcome is a property of
# :func:`run` (output-parsing), NOT this span — this span only reports
# the subprocess result (ok/timeout/no_endpoints/error).
_tracer = _otel_trace.get_tracer("caretaker.pr_reviewer.opencode_local")


def _resolve_tier_model(
    tier: ComplexityTier | None,
    *,
    tier_map: dict[str, str],
    default: str,
) -> str:
    """Pick a model from a tier map, falling back to the default.

    Empty tier-map entries (or unknown tier names) fall through to
    ``default`` so a typo'd config doesn't break the run.
    """
    if tier is None:
        return default
    return tier_map.get(tier, "") or default


class OpenCodeLocalError(RuntimeError):
    """Raised when the local opencode CLI run fails (clone, invocation, parse)."""


class OpenCodeLocalTimeoutError(OpenCodeLocalError):
    """Raised specifically when the opencode subprocess timed out."""


class OpenCodeLocalNoEndpointsError(OpenCodeLocalError):
    """Raised when opencode reports ``No endpoints found`` (provider misconfig).

    Distinguished from generic ``OpenCodeLocalError`` so the metrics
    layer can attribute the failure to the right outcome bucket without
    string-matching stderr at every callsite.
    """


def _parse_pr_url_wrapped(pr_url: str):  # type: ignore[no-untyped-def]
    """Translate ``WorkdirError`` → ``OpenCodeLocalError``."""
    try:
        return parse_pr_url(pr_url)
    except WorkdirError as exc:
        raise OpenCodeLocalError(str(exc)) from exc


async def _prepare_workdir(
    pr_url: str,
    *,
    config: OpenCodeLocalBackendConfig,
    head_branch: str | None = None,
) -> tuple[str, object]:
    token = (config.extra_env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    return await prepare_workdir(
        pr_url,
        clone_depth=config.clone_depth,
        workdir_root=config.clone_workdir_root or None,
        head_branch=head_branch,
        github_token=token or None,
    )


_REVIEW_PROMPT = """\
You are reviewing a pull request that has been freshly cloned into the
current working directory. The PR head is checked out as the current
branch (``caretaker/pr-head``). The default base branch is the remote
default (run ``git remote show origin`` to confirm).

Steps:
  1. Identify the changed files via ``git diff --name-only origin/HEAD``.
  2. Read each changed file in full.
  3. Walk neighbouring code to understand call sites and context.
  4. Evaluate: correctness, security, API/back-compat, test coverage.

Output ONE message with ONLY the following two parts, in order:

  1. A short prose summary of your findings (2-6 sentences).

  2. The exact marker ``<!-- caretaker:review-result -->`` on its own
     line, followed by a fenced JSON block tagged ``caretaker-review``
     with this schema (no comments, strict JSON):

     ```caretaker-review
     {
       "verdict": "APPROVE" | "COMMENT" | "REQUEST_CHANGES",
       "summary": "1-3 sentence overall assessment",
       "comments": [
         {"path": "src/foo.py", "line": 42, "body": "..."}
       ],
       "issue_categories": ["lint", "format", "type", "test",
                            "security", "correctness", "docs", "other"]
     }
     ```

Pick ``REQUEST_CHANGES`` only for blocking issues (security,
correctness, broken tests). ``COMMENT`` for non-blocking observations.
``APPROVE`` only when you have no concerns at all. Cap inline
``comments`` at 8 entries; line numbers refer to the new file
(right-hand side of the diff). When verdict is ``REQUEST_CHANGES``,
fill ``issue_categories`` with one or more of the allowed values above
(ordered by impact, most dominant first) so the auto-fix dispatcher
can route cheap issues (``lint``/``format``) to a deterministic fixer
and expensive ones (``security``/``correctness``) to a heavy agent.
"""


def _truncate(line: str, max_len: int) -> str:
    if len(line) <= max_len:
        return line
    return line[:max_len] + f"… ({len(line) - max_len} more chars)"


def _parse_opencode_json_stream(
    stdout: str,
) -> tuple[str, int, int]:
    """Parse opencode ``--format json`` stdout into (text, prompt_tokens, completion_tokens).

    opencode emits one JSON object per line on stdout when invoked with
    ``--format json``.  Three event types matter for our purposes:

    * ``text``       — fragments of the assistant's response. We
      concatenate the ``part.text`` strings in stream order to
      reconstruct the prose+JSON-block payload the existing parser
      expects.
    * ``step_finish`` — emitted at the end of each agent step with a
      ``part.tokens`` object: ``{input, output, reasoning, cache: {read, write}}``.
      We sum ``input`` (with cache reads added back in — those are
      still input tokens that count against the operator's bill) and
      ``output + reasoning`` for prompt vs. completion tokens.
    * everything else (tool_use, step_start, error, …) is ignored for
      token-accounting purposes.

    Lines that don't parse as JSON are silently skipped — opencode
    sometimes emits a final ANSI-colored summary line on stdout even
    in JSON mode (the ``> build · …`` banner), and we don't want a
    parse failure there to fail the whole invocation.
    """
    text_parts: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        evt_type = evt.get("type")
        part = evt.get("part") if isinstance(evt.get("part"), dict) else {}
        if evt_type == "text":
            txt = part.get("text") if isinstance(part, dict) else None
            if isinstance(txt, str):
                text_parts.append(txt)
        elif evt_type == "step_finish":
            tokens = part.get("tokens") if isinstance(part, dict) else None
            if not isinstance(tokens, dict):
                continue
            inp = tokens.get("input") or 0
            out = tokens.get("output") or 0
            reasoning = tokens.get("reasoning") or 0
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            cache_read = cache.get("read") or 0 if isinstance(cache, dict) else 0
            cache_write = cache.get("write") or 0 if isinstance(cache, dict) else 0
            # ``input`` from opencode excludes cache reads/writes when
            # caching is in use; we add them back so the prompt-token
            # count matches what the provider actually billed.  Reasoning
            # tokens are part of the model's output bill, so they go on
            # the completion side.
            if isinstance(inp, int | float):
                prompt_tokens += int(inp)
            if isinstance(cache_read, int | float):
                prompt_tokens += int(cache_read)
            if isinstance(cache_write, int | float):
                prompt_tokens += int(cache_write)
            if isinstance(out, int | float):
                completion_tokens += int(out)
            if isinstance(reasoning, int | float):
                completion_tokens += int(reasoning)
    return "".join(text_parts), prompt_tokens, completion_tokens


async def _invoke_opencode(
    *,
    workdir: str,
    config: OpenCodeLocalBackendConfig,
    prompt: str = "",
    model_override: str = "",
) -> str:
    """Spawn the opencode CLI in ``workdir`` and return the assistant text.

    opencode is invoked with ``--format json`` so each agent event lands
    on stdout as a JSON object on its own line. We assemble the
    assistant's prose + ``caretaker-review`` JSON block from the
    ``text`` events and sum prompt/completion token counts from the
    ``step_finish`` events for cost-tracking metrics.

    The default-format banner output (``> build · …``) is replaced with
    the structured event stream; the existing ``_parse_review_payload``
    contract still receives a plain text string and looks for the
    ``caretaker-review`` fence in it.
    """
    with _tracer.start_as_current_span("opencode_local.invoke") as span:
        span.set_attribute("caretaker.opencode.workdir", workdir)
        span.set_attribute("caretaker.opencode.timeout_seconds", int(config.timeout_seconds))

        resolved = shutil.which(config.cli_path) or config.cli_path
        if not os.path.isabs(resolved) and not shutil.which(resolved):
            exc = OpenCodeLocalError(
                f"opencode CLI not found at {config.cli_path!r}; install it "
                "(`npm install -g opencode-ai` or pin a path in "
                "pr_reviewer.opencode_local.cli_path). "
                "Requires OPENROUTER_API_KEY in the environment."
            )
            span.set_attribute("caretaker.opencode.outcome", "error")
            with contextlib.suppress(
                Exception
            ):  # pragma: no cover - never let tracer mask original
                span.record_exception(exc)
                span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR, str(exc)[:200]))
            raise exc

        env = os.environ.copy()
        if config.extra_env:
            env.update(config.extra_env)

        model = model_override or config.model
        span.set_attribute("caretaker.opencode.model", str(model or ""))
        # opencode CLI uses ``run`` as the non-interactive subcommand.
        # ``--format json`` emits one event per stdout line which gives
        # us per-step token counts for the cost-tracking metrics; the
        # assistant text is reassembled by ``_parse_opencode_json_stream``.
        args = [resolved, "run", prompt or _REVIEW_PROMPT, "--format", "json"]
        if model:
            args += ["--model", model]

        logger.info(
            "opencode_local: invoking %s model=%s in %s",
            resolved,
            model or "(opencode default)",
            workdir,
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await stream_subprocess_output(
                proc,
                timeout_seconds=config.timeout_seconds,
                stdout_log=lambda line: logger.info("opencode | %s", _truncate(line, 400)),
                stderr_log=lambda line: logger.warning("opencode! %s", line),
            )
        except TimeoutError as exc:
            timeout_err = OpenCodeLocalTimeoutError(
                f"opencode timed out after {config.timeout_seconds}s"
            )
            span.set_attribute("caretaker.opencode.outcome", "timeout")
            with contextlib.suppress(
                Exception
            ):  # pragma: no cover - never let tracer mask original
                span.record_exception(timeout_err)
                span.set_status(
                    _otel_trace.Status(_otel_trace.StatusCode.ERROR, str(timeout_err)[:200])
                )
            raise timeout_err from exc
        span.set_attribute("caretaker.opencode.exit_code", int(proc.returncode or 0))
        if proc.returncode != 0:
            # Special-case: opencode emits ``No endpoints found`` when the
            # configured provider (OpenRouter) hasn't been wired up. We
            # surface this as its own exception type so the caller can
            # record a specific ``no_endpoints`` outcome on the metric.
            if "No endpoints found" in stderr:
                no_ep_err = OpenCodeLocalNoEndpointsError(
                    f"opencode exited {proc.returncode} with No endpoints found "
                    f"(check OPENROUTER_API_KEY / model id): {stderr.strip()[:500]}"
                )
                span.set_attribute("caretaker.opencode.outcome", "no_endpoints")
                with contextlib.suppress(
                    Exception
                ):  # pragma: no cover - never let tracer mask original
                    span.record_exception(no_ep_err)
                    span.set_status(
                        _otel_trace.Status(_otel_trace.StatusCode.ERROR, str(no_ep_err)[:200])
                    )
                raise no_ep_err
            err = OpenCodeLocalError(
                f"opencode exited {proc.returncode}: {stderr.strip() or stdout.strip()[:500]}"
            )
            span.set_attribute("caretaker.opencode.outcome", "error")
            with contextlib.suppress(
                Exception
            ):  # pragma: no cover - never let tracer mask original
                span.record_exception(err)
                span.set_status(_otel_trace.Status(_otel_trace.StatusCode.ERROR, str(err)[:200]))
            raise err

        # Subprocess exited cleanly. parse_fallback (if any) is determined
        # by :func:`run`'s output parser, not this span — see module docstring.
        span.set_attribute("caretaker.opencode.outcome", "ok")

        # Parse the JSON event stream into (assistant_text, p_tokens, c_tokens).
        # Token-extraction failures must NOT fail the run — log at DEBUG
        # and fall back to the raw stdout. The assistant text *should*
        # always parse out of the stream, but if opencode ever changes
        # its output format we still want the review to land.
        try:
            assistant_text, prompt_tokens, completion_tokens = _parse_opencode_json_stream(stdout)
        except Exception:  # pragma: no cover - defence in depth
            logger.debug(
                "opencode_local: token extraction from JSON stream failed; returning raw stdout",
                exc_info=True,
            )
            return stdout

        if prompt_tokens > 0 or completion_tokens > 0:
            span.set_attribute("caretaker.llm.prompt_tokens", prompt_tokens)
            span.set_attribute("caretaker.llm.completion_tokens", completion_tokens)
            with contextlib.suppress(Exception):
                record_llm_usage(
                    model=model or "unknown",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
        else:
            logger.debug(
                "opencode_local: no token usage extracted from JSON stream "
                "(model=%s); skipping LLM usage metrics",
                model,
            )

        # Fall back to raw stdout if no text events appeared — keeps the
        # downstream parser robust against an unusual opencode reply.
        return assistant_text or stdout


_RESULT_TEXT_RE = re.compile(r"```caretaker-review\s*\n(?P<json>.+?)\n\s*```", re.DOTALL)


def _parse_review_payload(assistant_text: str) -> tuple[ReviewResult, bool]:
    """Find the ``caretaker-review`` JSON block; fall back to a generic COMMENT review.

    Returns ``(result, was_fallback)`` so the caller can record the
    parse-fallback outcome with the real model + mode labels (instead
    of double-counting via an in-function metric increment that loses
    label fidelity). The WARNING log stays here — that's still useful
    for log-based alerting independent of the metric.

    The fallback ensures a noisy or non-conforming opencode reply still
    produces *some* review on the PR rather than dropping the work.
    """
    match = _RESULT_TEXT_RE.search(assistant_text)
    if not match:
        logger.warning(
            "opencode_local: fallback parse used — structured caretaker-review JSON missing. "
            "stdout_bytes=%d sample=%r",
            len(assistant_text),
            assistant_text[:200],
        )
        return (
            ReviewResult(
                summary=(
                    "**Review by opencode_local (fallback parse)**\n\n"
                    "opencode did not emit a structured `caretaker-review` JSON block; "
                    "its prose response is included below.\n\n"
                    f"{assistant_text.strip()[:4000]}"
                ),
                verdict="COMMENT",
                comments=[],
            ),
            True,
        )
    raw = match.group("json").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenCodeLocalError(f"caretaker-review block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OpenCodeLocalError("caretaker-review payload is not a JSON object")

    summary = payload.get("summary", "").strip() if isinstance(payload.get("summary"), str) else ""
    verdict = payload.get("verdict", "COMMENT")
    if verdict not in {"APPROVE", "COMMENT", "REQUEST_CHANGES"}:
        verdict = "COMMENT"
    raw_comments = payload.get("comments") or []
    comments: list[InlineReviewComment] = []
    if isinstance(raw_comments, list):
        for entry in raw_comments[:8]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            line = entry.get("line")
            body = entry.get("body")
            if (
                isinstance(path, str)
                and path
                and isinstance(line, int)
                and line > 0
                and isinstance(body, str)
                and body.strip()
            ):
                comments.append(InlineReviewComment(path=path, line=line, body=body.strip()))

    raw_categories = payload.get("issue_categories") or []
    issue_categories: list[str] = []
    if isinstance(raw_categories, list):
        _valid = {
            "lint",
            "format",
            "type",
            "test",
            "security",
            "correctness",
            "docs",
            "other",
        }
        for c in raw_categories:
            if isinstance(c, str) and c in _valid and c not in issue_categories:
                issue_categories.append(c)

    body = (
        "**Review by opencode_local "
        "(opencode CLI in caretaker's pod)**\n\n"
        f"{summary or 'opencode returned no summary text.'}"
    )
    return (
        ReviewResult(
            summary=body, verdict=verdict, comments=comments, issue_categories=issue_categories
        ),
        False,
    )


async def run(
    *,
    pr_url: str,
    config: OpenCodeLocalBackendConfig,
    tier: ComplexityTier | None = None,
) -> ReviewResult:
    """Backend runner — clone, invoke opencode, parse, return ``ReviewResult``.

    When ``tier`` is supplied, the model is resolved from
    ``config.review_models[tier]`` (with ``config.model`` as fallback)
    so cheap PRs route to cheap models without operator intervention.
    """
    workdir: str | None = None
    success = False
    review_model = _resolve_tier_model(tier, tier_map=config.review_models, default=config.model)
    try:
        workdir, _parsed = await _prepare_workdir(pr_url, config=config)
        stdout = await _invoke_opencode(
            workdir=workdir,
            config=config,
            prompt=_REVIEW_PROMPT,
            model_override=review_model,
        )
        # ``_parse_review_payload`` returns a (result, was_fallback)
        # tuple so we record the outcome exactly once with the real
        # model + mode labels (no in-function double-count).
        result, used_fallback = _parse_review_payload(stdout)
        success = True
        record_opencode_invocation(
            model=review_model,
            mode="review",
            outcome="parse_fallback" if used_fallback else "ok",
        )
        return result
    except OpenCodeLocalTimeoutError:
        record_opencode_invocation(model=review_model, mode="review", outcome="timeout")
        raise
    except OpenCodeLocalNoEndpointsError:
        record_opencode_invocation(model=review_model, mode="review", outcome="no_endpoints")
        raise
    except WorkdirError as exc:
        record_opencode_invocation(model=review_model, mode="review", outcome="error")
        raise OpenCodeLocalError(str(exc)) from exc
    except Exception:
        record_opencode_invocation(model=review_model, mode="review", outcome="error")
        raise
    finally:
        if workdir is not None:
            cleanup_workdir(workdir, keep=(not success and config.keep_workdir_on_failure))


_FIX_PROMPT_TEMPLATE = """\
You are addressing review feedback on a pull request. The repo is
already cloned and checked out to the PR's head branch in your current
working directory; your edits commit directly to that branch.

The reviewer's verdict was REQUEST_CHANGES with the following summary:

---
{summary}
---

{comments_section}

Your job:
  1. Address each issue. Make the smallest change that fixes the
     concern; do not refactor unrelated code.
  2. If a test was missing or broken, write/fix it.
  3. Do NOT change behaviour beyond what the review asked for.
  4. Run any obvious local validation (e.g. ``ruff check``,
     ``ruff format``) before finishing if those tools are configured
     in the repo.
  5. After making changes, output one short summary line describing
     what you changed. Do NOT output any JSON — caretaker handles the
     commit + push.

If you decide the review is incorrect or the change is unsafe to make
automatically, output exactly the line ``CARETAKER_FIX_DECLINED:``
followed by a one-sentence explanation, and make no file changes.
"""


def _build_fix_prompt(*, summary: str, comments: list[InlineReviewComment]) -> str:
    if comments:
        rendered = "\n".join(f"- `{c.path}:{c.line}` — {c.body}" for c in comments[:8])
        comments_section = f"Inline comments:\n{rendered}\n"
    else:
        comments_section = "(no inline comments)\n"
    return _FIX_PROMPT_TEMPLATE.format(
        summary=summary.strip() or "(no summary)", comments_section=comments_section
    )


async def fix_run(
    *,
    workdir: str,
    review_summary: str,
    review_comments: list[InlineReviewComment],
    config: OpenCodeLocalBackendConfig,
    tier: ComplexityTier | None = None,
) -> str:
    """Invoke opencode in fix mode against an already-prepared workdir.

    Returns the assistant's final summary text. The caller decides
    whether to commit + push by inspecting ``git diff`` on the workdir.

    When ``tier`` is supplied, the model is resolved from
    ``config.fix_models[tier]`` (with ``config.fix_model`` then
    ``config.model`` as fallbacks).  Trivial/simple fixes route to
    cheap models without changing the prompt.

    Raises :class:`OpenCodeLocalError` on subprocess failure or on the
    sentinel ``CARETAKER_FIX_DECLINED:`` return.
    """
    prompt = _build_fix_prompt(summary=review_summary, comments=review_comments)
    fix_model = _resolve_tier_model(
        tier,
        tier_map=getattr(config, "fix_models", {}),
        default=getattr(config, "fix_model", "") or config.model,
    )
    try:
        stdout = await _invoke_opencode(
            workdir=workdir,
            config=config,
            prompt=prompt,
            model_override=fix_model,
        )
    except OpenCodeLocalTimeoutError:
        record_opencode_invocation(model=fix_model, mode="fix", outcome="timeout")
        raise
    except OpenCodeLocalNoEndpointsError:
        record_opencode_invocation(model=fix_model, mode="fix", outcome="no_endpoints")
        raise
    except Exception:
        record_opencode_invocation(model=fix_model, mode="fix", outcome="error")
        raise

    # Inspect the in-band sentinel BEFORE recording the outcome so a
    # decline doesn't pollute the success rate. ``declined`` is its
    # own outcome bucket — see :data:`OPENCODE_OUTCOMES`.
    text = stdout.strip()
    if text.startswith("CARETAKER_FIX_DECLINED:"):
        record_opencode_invocation(model=fix_model, mode="fix", outcome="declined")
        raise OpenCodeLocalError(f"opencode declined to fix: {text}")
    record_opencode_invocation(model=fix_model, mode="fix", outcome="ok")
    return text or "(no summary)"


SPEC = HandoffReviewerSpec(
    backend="opencode_local",
    marker=OPENCODE_LOCAL_REVIEW_MARKER,
    upstream_action_name="opencode CLI (in caretaker pod)",
    label_color="d04a02",
    label_description="opencode_local review (in-pod subprocess)",
    invocation="local_subprocess",
    runner=run,
)


__all__ = [
    "SPEC",
    "OpenCodeLocalError",
    "fix_run",
    "run",
]
