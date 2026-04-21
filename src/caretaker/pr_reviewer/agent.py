"""PRReviewerAgent — dual-path PR code reviewer.

Subscribes to ``pull_request`` opened/synchronize events.  For each new or
updated PR it:

1. Scores the PR using :mod:`routing` (LOC, file count, sensitive patterns, labels).
2. Routes to the inline LLM reviewer  (score < threshold) or
   the ``claude-code-action`` hand-off (score >= threshold).
3. Posts the review via the GitHub Reviews API (inline path) or
   applies a trigger label + hand-off comment (claude-code path).

The agent is opt-in: ``pr_reviewer.enabled = false`` (default) keeps
existing behavior byte-identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from caretaker.agent_protocol import AgentResult, BaseAgent
from caretaker.pr_reviewer import claude_code_reviewer, inline_reviewer
from caretaker.pr_reviewer.github_review import post_review
from caretaker.pr_reviewer.routing import decide

if TYPE_CHECKING:
    from caretaker.state.models import OrchestratorState

logger = logging.getLogger(__name__)

_HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})


@dataclass
class _PRReviewReport:
    reviewed: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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
        report = _PRReviewReport()

        action = (event_payload or {}).get("action", "")
        if action and action not in _HANDLED_ACTIONS:
            return AgentResult(processed=0)

        pr_data = (event_payload or {}).get("pull_request") if event_payload else None

        if pr_data:
            prs = [pr_data]
        else:
            # Polling fallback: review open PRs that have not been reviewed yet
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
                await self._handle_pr(pr, report)
            except Exception as exc:
                err = f"pr-reviewer: unhandled error on #{pr_number}: {exc}"
                logger.exception(err)
                report.errors.append(err)

        return AgentResult(
            processed=len(report.reviewed) + len(report.dispatched),
            errors=report.errors,
            extra={
                "reviewed": report.reviewed,
                "dispatched": report.dispatched,
                "skipped": report.skipped,
            },
        )

    async def _handle_pr(
        self,
        pr: dict[str, Any],
        report: _PRReviewReport,
    ) -> None:
        cfg = self._ctx.config.pr_reviewer
        pr_number = int(pr.get("number", 0))
        owner = self._ctx.owner
        repo = self._ctx.repo

        # Skip drafts
        if cfg.skip_draft and pr.get("draft", False):
            report.skipped.append(pr_number)
            return

        # Skip if already reviewed by caretaker this cycle
        pr_labels = [
            lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
            for lbl in pr.get("labels", [])
        ]
        if any(lbl in cfg.skip_labels for lbl in pr_labels):
            report.skipped.append(pr_number)
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
        )
        logger.info("pr-reviewer: #%d routing — %s", pr_number, decision.reason)

        if decision.use_inline:
            if self._ctx.llm_router is None or not self._ctx.llm_router.available:
                logger.warning(
                    "pr-reviewer: LLM unavailable for inline review of #%d, falling back",
                    pr_number,
                )
                decision = decision  # fall through to claude-code below
            else:
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
                commit_sha = (pr.get("head") or {}).get("sha", "")
                if not commit_sha:
                    logger.warning("pr-reviewer: no head SHA for #%d", pr_number)
                    report.skipped.append(pr_number)
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
                return

        # Claude-code hand-off path
        success = await claude_code_reviewer.dispatch(
            github=self._ctx.github,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            config=cfg,
            routing_reason=decision.reason,
        )
        if success:
            report.dispatched.append(pr_number)
        else:
            report.errors.append(f"claude-code dispatch failed for #{pr_number}")

    def apply_summary(self, result: AgentResult, summary: Any) -> None:  # type: ignore[override]
        pass
