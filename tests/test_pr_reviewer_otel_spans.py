"""Custom OTel spans for the PR-review pipeline.

Each span emitted by the instrumented helpers (root ``pr_reviewer.handle_pr``
plus the per-stage children) is exercised here using OTel's
``InMemorySpanExporter`` so test assertions can read the live
attributes set by the production code without touching the network or
the OTel collector.

These tests pin the *names* and *attribute keys* of the spans rather
than every value, because the values are already covered by the
behavioural tests in ``test_pr_reviewer*``. The point of this file is:

    "Did the production span emission survive a refactor?"

NOTE: this file uses a process-global ``TracerProvider``. OTel's
``set_tracer_provider`` is once-per-process, so the provider + exporter
are scoped to the *module* and only the in-memory buffer is cleared
between tests. Do NOT run this file with pytest-xdist — concurrent
workers would race on the shared provider and exporter buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# Skip the whole module if the OTel SDK isn't installed — these tests
# exercise the real provider, not the no-op fallback.
pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture(scope="module")
def _tracer_provider_with_exporter() -> Iterator[InMemorySpanExporter]:
    """Module-scoped: install one TracerProvider + exporter for the whole file.

    OTel's ``set_tracer_provider`` is once-per-process, and adding a new
    ``SpanProcessor`` per test would leak — old processors would keep
    receiving spans and writing to dead exporters. Installing the
    plumbing once at module scope sidesteps both issues.
    """
    exporter = InMemorySpanExporter()
    current = otel_trace.get_tracer_provider()
    if hasattr(current, "add_span_processor"):
        # An SDK provider is already installed (e.g. by an earlier
        # module's fixture). Attach our exporter to it.
        current.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
    yield exporter


@pytest.fixture
def span_exporter(_tracer_provider_with_exporter: InMemorySpanExporter) -> Iterator[Any]:
    """Function-scoped: clear the in-memory buffer between tests."""
    yield _tracer_provider_with_exporter
    _tracer_provider_with_exporter.clear()


def _spans_by_name(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    """Return the captured spans whose ``name`` matches exactly."""
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ── 1. Root span: pr_reviewer.handle_pr ───────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_emits_root_span(span_exporter: InMemorySpanExporter) -> None:
    """Happy path: ``_handle_pr`` emits one root ``pr_reviewer.handle_pr`` span."""
    from caretaker.config import PRReviewerConfig
    from caretaker.pr_reviewer.agent import PRReviewerAgent

    mock_ctx = MagicMock()
    mock_ctx.config.pr_reviewer = PRReviewerConfig(
        enabled=True,
        webhook_only=False,
        trigger_actions=["opened"],
        skip_draft=True,
        skip_labels=[],
    )
    mock_ctx.owner = "org"
    mock_ctx.repo = "repo"
    mock_ctx.llm_router = None
    mock_ctx.github = MagicMock()

    agent = PRReviewerAgent(mock_ctx)
    # Use a draft PR so handle_pr exits early without touching the github
    # client beyond the audit emit — keeps the test simple while still
    # exercising the span open/close.
    await agent.execute(
        state=MagicMock(),
        event_payload={
            "action": "opened",
            "pull_request": {
                "number": 7,
                "title": "Draft",
                "body": "",
                "draft": True,
                "head": {"sha": "deadbeef"},
                "labels": [],
                "user": {"login": "human"},
            },
        },
    )

    spans = _spans_by_name(span_exporter, "pr_reviewer.handle_pr")
    assert len(spans) == 1, [s.name for s in span_exporter.get_finished_spans()]
    span = spans[0]
    assert span.attributes["caretaker.pr.repo"] == "org/repo"
    assert span.attributes["caretaker.pr.number"] == 7
    assert span.attributes["caretaker.pr.author"] == "human"
    assert span.attributes["caretaker.pr.is_caretaker_owned"] is False
    # Verdict gets stamped via _emit_pr_review_audit on the way out.
    assert span.attributes["caretaker.review.verdict"] == "skipped"


@pytest.mark.asyncio
async def test_root_span_records_exception(span_exporter: InMemorySpanExporter) -> None:
    """When ``_handle_pr`` raises, the root span is recorded with status=ERROR."""
    from caretaker.config import PRReviewerConfig
    from caretaker.pr_reviewer.agent import PRReviewerAgent

    mock_ctx = MagicMock()
    mock_ctx.config.pr_reviewer = PRReviewerConfig(
        enabled=True,
        webhook_only=False,
        trigger_actions=["opened"],
        skip_draft=False,
        skip_labels=[],
    )
    mock_ctx.owner = "org"
    mock_ctx.repo = "repo"
    mock_ctx.llm_router = None
    mock_ctx.github = MagicMock()
    # Make list_pull_request_files raise so _handle_pr_body proceeds with
    # files=[] (graceful) — but to actually test exception recording we
    # need to force a raise. Patch _is_caretaker_owned via the import to
    # raise.
    from caretaker.pr_reviewer import agent as agent_module

    def _boom(_pr: dict[str, Any]) -> bool:
        raise RuntimeError("boom from test")

    original = agent_module._is_caretaker_owned
    agent_module._is_caretaker_owned = _boom  # type: ignore[assignment]
    try:
        agent = PRReviewerAgent(mock_ctx)
        # ``execute`` swallows the exception per-PR (logged), but the
        # span exception/status is set inside _handle_pr before the unwind.
        await agent.execute(
            state=MagicMock(),
            event_payload={
                "action": "opened",
                "pull_request": {
                    "number": 11,
                    "title": "Boom",
                    "body": "",
                    "draft": False,
                    "head": {"sha": "x"},
                    "labels": [],
                    "user": {"login": "human"},
                },
            },
        )
    finally:
        agent_module._is_caretaker_owned = original  # type: ignore[assignment]

    spans = _spans_by_name(span_exporter, "pr_reviewer.handle_pr")
    assert len(spans) == 1
    span = spans[0]
    # OTel SDK marks recorded exceptions as events on the span and sets
    # status to ERROR on .set_status(...).
    assert span.status.status_code == otel_trace.StatusCode.ERROR
    assert any(evt.name == "exception" for evt in span.events)


# ── 2. complexity_classifier.classify ─────────────────────────────────────


@pytest.mark.asyncio
async def test_complexity_classifier_emits_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    """``classify`` emits a span with tier + source attributes."""
    from caretaker.evolution.executor_routing import (
        ExecutorRouteContext,
        ExecutorRouteFile,
    )
    from caretaker.pr_reviewer.complexity_classifier import classify

    context = ExecutorRouteContext(
        task_type="pr_review",
        files=[ExecutorRouteFile(path="src/foo.py", additions=2, deletions=1)],
        labels=[],
        repo_slug="org/repo",
        title="tiny",
        body="",
    )
    # Tiny diff → fast_path returns "trivial" without an LLM call.
    verdict = await classify(context=context, claude=None)
    assert verdict.tier == "trivial"

    spans = _spans_by_name(span_exporter, "complexity_classifier.classify")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["caretaker.pr.repo"] == "org/repo"
    assert attrs["caretaker.complexity.file_count"] == 1
    assert attrs["caretaker.complexity.tier"] == "trivial"
    assert attrs["caretaker.complexity.source"] == "fast_path"
    assert attrs["caretaker.complexity.confidence"] == pytest.approx(0.9)


# ── 3. pr_review.clone_workdir ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clone_workdir_emits_span(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``prepare_workdir`` emits ``pr_review.clone_workdir`` with attributes."""
    from caretaker.pr_reviewer.backends import _workdir as workdir_mod

    # Stub out _run_git so the test stays purely in-process.
    async def _fake_run_git(*_args: str, **_kwargs: Any) -> str:
        # When asked for HEAD SHA, return a fake one; otherwise empty.
        if _args and _args[0] == "rev-parse":
            return "abc1234567890\n"
        return ""

    monkeypatch.setattr(workdir_mod, "_run_git", _fake_run_git)
    monkeypatch.setattr(workdir_mod.tempfile, "mkdtemp", lambda **_kw: str(tmp_path))
    # Avoid creating ``repo`` subdir on disk since we never run real git.
    monkeypatch.setattr(workdir_mod.os.path, "join", lambda *parts: "/".join(parts))

    repo_dir, parsed = await workdir_mod.prepare_workdir(
        "https://github.com/o/r/pull/9", clone_depth=3
    )
    assert parsed.number == 9

    spans = _spans_by_name(span_exporter, "pr_review.clone_workdir")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["caretaker.pr.url"] == "https://github.com/o/r/pull/9"
    assert attrs["caretaker.workdir.clone_depth"] == 3
    assert attrs["caretaker.workdir.path"] == str(tmp_path)
    # Best-effort head SHA capture.
    assert attrs.get("caretaker.pr.head_sha") == "abc1234567890"


# ── 4. opencode_local.invoke ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_opencode_local_invoke_emits_span(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_invoke_opencode` emits a span with model + exit_code + outcome=ok."""
    from caretaker.pr_reviewer.backends import opencode_local

    fake_proc = MagicMock()
    fake_proc.returncode = 0

    async def _fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> Any:
        return fake_proc

    monkeypatch.setattr(
        opencode_local.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        opencode_local,
        "stream_subprocess_output",
        AsyncMock(return_value=("hello stdout", "")),
    )
    monkeypatch.setattr(opencode_local.shutil, "which", lambda _name: "/usr/bin/opencode")

    cfg = MagicMock()
    cfg.cli_path = "opencode"
    cfg.extra_env = {}
    cfg.timeout_seconds = 60
    cfg.model = "openrouter/test/model"

    out = await opencode_local._invoke_opencode(
        workdir="/tmp/fake", config=cfg, prompt="hi", model_override=""
    )
    assert out == "hello stdout"

    spans = _spans_by_name(span_exporter, "opencode_local.invoke")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["caretaker.opencode.workdir"] == "/tmp/fake"
    assert attrs["caretaker.opencode.timeout_seconds"] == 60
    assert attrs["caretaker.opencode.model"] == "openrouter/test/model"
    assert attrs["caretaker.opencode.exit_code"] == 0
    assert attrs["caretaker.opencode.outcome"] == "ok"


# ── 5. auto_fix.dispatch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_fix_dispatch_emits_span(
    span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful ``dispatch_auto_fix`` emits ``auto_fix.dispatch`` span."""
    from caretaker.config import AutoFixConfig, PRReviewerConfig
    from caretaker.pr_reviewer import auto_fix as auto_fix_mod
    from caretaker.pr_reviewer.auto_fix import (
        DETERMINISTIC_LINT_BACKEND,
        AutoFixDecision,
        dispatch_auto_fix,
    )
    from caretaker.pr_reviewer.inline_reviewer import ReviewResult
    from caretaker.state.models import TrackedPR

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    # Stub out the entire workdir + lint + commit chain so the test stays
    # in-process and exercises only the span wiring around the dispatch.
    from caretaker.pr_reviewer.backends import _workdir as workdir_mod

    monkeypatch.setattr(
        workdir_mod,
        "prepare_workdir",
        AsyncMock(return_value=("/tmp/fake-workdir", MagicMock())),
    )
    monkeypatch.setattr(workdir_mod, "cleanup_workdir", MagicMock())
    monkeypatch.setattr(auto_fix_mod, "run_deterministic_lint", AsyncMock(return_value=True))
    monkeypatch.setattr(auto_fix_mod, "commit_and_push", AsyncMock(return_value="newhead123"))
    monkeypatch.setattr(auto_fix_mod, "post_dispatch_comment", AsyncMock())

    config = PRReviewerConfig(auto_fix=AutoFixConfig(enabled=True))
    decision = AutoFixDecision(
        should_dispatch=True,
        backend=DETERMINISTIC_LINT_BACKEND,
        reason="test",
        categories=["lint"],
    )
    review = ReviewResult(summary="x", verdict="REQUEST_CHANGES")
    tracking = TrackedPR(number=1)

    outcome = await dispatch_auto_fix(
        decision=decision,
        pr_url="https://github.com/o/r/pull/1",
        head_branch="feature",
        review=review,
        config=config,
        github=MagicMock(),
        owner="o",
        repo="r",
        pr_number=1,
        tracking=tracking,
    )
    assert outcome.success is True
    assert outcome.new_head_sha == "newhead123"

    spans = _spans_by_name(span_exporter, "auto_fix.dispatch")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["caretaker.pr.repo"] == "o/r"
    assert attrs["caretaker.pr.number"] == 1
    assert attrs["caretaker.auto_fix.backend"] == DETERMINISTIC_LINT_BACKEND
    assert tuple(attrs["caretaker.auto_fix.categories"]) == ("lint",)
    assert attrs["caretaker.auto_fix.attempt"] == 1
    assert attrs["caretaker.auto_fix.outcome"] == "success"
    assert attrs["caretaker.auto_fix.new_head_sha"] == "newhead123"


# ── 6. inline_reviewer.review ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_reviewer_emits_span(span_exporter: InMemorySpanExporter) -> None:
    """``review`` emits ``inline_reviewer.review`` with diff_lines + verdict."""
    from caretaker.pr_reviewer.inline_reviewer import (
        InlineReviewResult,
        review,
    )

    github = MagicMock()
    github.get_pull_diff = AsyncMock(return_value="diff --git a/foo b/foo\n+added\n-removed\n")

    fake_payload = InlineReviewResult(
        summary="lgtm",
        verdict="APPROVE",
        comments=[],
        issue_categories=[],
    )
    llm = MagicMock()
    llm.claude.model = "claude-test-model"
    llm.claude.structured_complete = AsyncMock(return_value=fake_payload)

    result = await review(
        github=github,
        owner="org",
        repo="repo",
        pr_number=42,
        pr_title="t",
        pr_body="b",
        llm=llm,
        max_diff_lines=2000,
    )
    assert result.verdict == "APPROVE"

    spans = _spans_by_name(span_exporter, "inline_reviewer.review")
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["caretaker.pr.repo"] == "org/repo"
    assert attrs["caretaker.pr.number"] == 42
    # 3 lines of diff in the canned response.
    assert attrs["caretaker.review.diff_lines"] == 3
    assert attrs["caretaker.review.model"] == "claude-test-model"
    assert attrs["caretaker.review.verdict"] == "APPROVE"


# ── nesting (best-effort) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complexity_classifier_nests_under_handle_pr(
    span_exporter: InMemorySpanExporter,
) -> None:
    """When called inside the root span, the classifier span has a parent."""
    from caretaker.evolution.executor_routing import (
        ExecutorRouteContext,
        ExecutorRouteFile,
    )
    from caretaker.pr_reviewer.complexity_classifier import classify

    tracer = otel_trace.get_tracer("test.parent")
    context = ExecutorRouteContext(
        task_type="pr_review",
        files=[ExecutorRouteFile(path="x.py", additions=1, deletions=0)],
        labels=[],
        repo_slug="org/repo",
        title="t",
        body="",
    )
    with tracer.start_as_current_span("pr_reviewer.handle_pr") as parent:
        parent_span_id = parent.get_span_context().span_id
        await classify(context=context, claude=None)

    child_spans = _spans_by_name(span_exporter, "complexity_classifier.classify")
    assert len(child_spans) == 1
    child = child_spans[0]
    assert child.parent is not None
    assert child.parent.span_id == parent_span_id
