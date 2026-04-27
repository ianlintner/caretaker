"""Cross-entity cascade cleanup for linked PRs and issues.

When a PR merges, any issue it resolves should close. When a PR closes without
merging, the issue it was attached to should return to the triage queue. When
an issue is closed as a duplicate, any PR targeting that issue should either
be redirected to the canonical issue or closed if it no longer has value.

The planners in this module are pure functions that emit ``CascadeAction``
records. The executor (``apply_cascade``) interprets the plan against a real
``GitHubClient``. This separation makes the logic straightforward to unit
test and keeps all GitHub side effects behind a single function.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import Issue, PullRequest
    from caretaker.state.models import TrackedIssue, TrackedPR

logger = logging.getLogger(__name__)


_CLOSES_KEYWORD_RE = re.compile(
    r"\b(?:closes|closed|close|fixes|fixed|fix|resolves|resolved|resolve)"
    r"\s+#(?P<number>\d+)",
    re.IGNORECASE,
)


def parse_linked_issues(pr_body: str) -> list[int]:
    """Return issue numbers referenced by closing keywords in the PR body."""
    if not pr_body:
        return []
    seen: set[int] = set()
    result: list[int] = []
    for match in _CLOSES_KEYWORD_RE.finditer(pr_body):
        number = int(match.group("number"))
        if number not in seen:
            seen.add(number)
            result.append(number)
    return result


class CascadeKind(StrEnum):
    CLOSE_ISSUE = "close_issue"
    UNLINK_ISSUE = "unlink_issue"
    COMMENT_ON_PR = "comment_on_pr"
    CLOSE_PR = "close_pr"


@dataclass(frozen=True)
class CascadeAction:
    kind: CascadeKind
    target: int
    reason: str
    # Optional reference to the entity that caused the cascade (PR#, issue#,
    # canonical duplicate target, etc.) — used to build close comments.
    source: int | None = None


@dataclass
class CascadeReport:
    applied: list[CascadeAction] = field(default_factory=list)
    skipped: list[tuple[CascadeAction, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def on_pr_merged(
    pr: PullRequest,
    tracked_issues: dict[int, TrackedIssue],
) -> list[CascadeAction]:
    """Plan cascade for a merged PR.

    For every open issue referenced by a closing keyword in the PR body, emit
    a ``CLOSE_ISSUE`` action. GitHub's native auto-close handles this in most
    cases, but bot-authored PRs often edit the body post-merge, leaving the
    linked issue orphaned — this backstops that drift.
    """
    actions: list[CascadeAction] = []
    for issue_number in parse_linked_issues(pr.body):
        tracked = tracked_issues.get(issue_number)
        if tracked is None:
            continue
        # Only close if the tracked state still treats the issue as open work.
        from caretaker.state.models import IssueTrackingState

        if tracked.state in (IssueTrackingState.CLOSED, IssueTrackingState.COMPLETED):
            continue
        actions.append(
            CascadeAction(
                kind=CascadeKind.CLOSE_ISSUE,
                target=issue_number,
                reason=f"Resolved by merged PR #{pr.number}",
                source=pr.number,
            )
        )
    return actions


def on_pr_closed_unmerged(
    pr: PullRequest,
    tracked_issues: dict[int, TrackedIssue],
) -> list[CascadeAction]:
    """Plan cascade for a PR that closed without merging.

    Any tracked issue whose ``assigned_pr`` points at this PR should be
    unlinked and returned to the triage queue; without that, the issue would
    look assigned forever.
    """
    actions: list[CascadeAction] = []
    for issue_number, tracked in tracked_issues.items():
        if tracked.assigned_pr != pr.number:
            continue
        actions.append(
            CascadeAction(
                kind=CascadeKind.UNLINK_ISSUE,
                target=issue_number,
                reason=f"PR #{pr.number} closed without merging",
                source=pr.number,
            )
        )
    return actions


def on_issue_closed_as_duplicate(
    issue: Issue,
    canonical_issue_number: int,
    tracked_prs: dict[int, TrackedPR],
    pr_bodies: dict[int, str] | None = None,
) -> list[CascadeAction]:
    """Plan cascade for an issue closed as duplicate of ``canonical_issue_number``.

    For every open PR whose body references the closed issue, emit a comment
    redirecting to the canonical issue. If the PR body contains *only* a
    reference to the closed issue (no substantive content), also emit a
    ``CLOSE_PR`` action to remove the now-pointless PR.
    """
    actions: list[CascadeAction] = []
    pr_bodies = pr_bodies or {}

    for pr_number in tracked_prs:
        body = pr_bodies.get(pr_number, "")
        linked = parse_linked_issues(body)
        if issue.number not in linked:
            continue

        actions.append(
            CascadeAction(
                kind=CascadeKind.COMMENT_ON_PR,
                target=pr_number,
                reason=(
                    f"Issue #{issue.number} was closed as a duplicate of "
                    f"#{canonical_issue_number}. This PR may need to be "
                    f"retargeted."
                ),
                source=canonical_issue_number,
            )
        )

        # PRs that reference only the duplicate and carry no real body text
        # become pointless once the target issue is gone. The heuristic here
        # is deliberately conservative: body must be short and contain no
        # other issue references.
        stripped = body.strip()
        if len(stripped) < 200 and len(linked) == 1:
            actions.append(
                CascadeAction(
                    kind=CascadeKind.CLOSE_PR,
                    target=pr_number,
                    reason=(
                        f"Target issue #{issue.number} closed as duplicate of "
                        f"#{canonical_issue_number}; no substantive work in PR body."
                    ),
                    source=canonical_issue_number,
                )
            )
    return actions


async def apply_cascade(
    github: GitHubClient,
    owner: str,
    repo: str,
    actions: list[CascadeAction],
    tracked_issues: dict[int, TrackedIssue],
    *,
    dry_run: bool = False,
) -> CascadeReport:
    """Execute a cascade plan against GitHub.

    The tracked-issue map is mutated in place: ``UNLINK_ISSUE`` clears the
    ``assigned_pr`` on the matching record so the next triage pass picks it up.
    """
    from caretaker.github_client.api import GitHubAPIError
    from caretaker.state.models import IssueTrackingState

    report = CascadeReport()

    for action in actions:
        if dry_run:
            report.skipped.append((action, "dry_run"))
            continue

        try:
            if action.kind is CascadeKind.CLOSE_ISSUE:
                comment = (
                    f"Closing: {action.reason}."
                    if action.source is None
                    else f"Closing — resolved by #{action.source}."
                )
                await github.add_issue_comment(owner, repo, action.target, comment)
                await github.update_issue(owner, repo, action.target, state="closed")
                tracked = tracked_issues.get(action.target)
                if tracked is not None:
                    tracked.state = IssueTrackingState.COMPLETED

            elif action.kind is CascadeKind.UNLINK_ISSUE:
                tracked = tracked_issues.get(action.target)
                if tracked is not None:
                    tracked.assigned_pr = None
                    tracked.state = IssueTrackingState.NEW

            elif action.kind is CascadeKind.COMMENT_ON_PR:
                await github.add_issue_comment(owner, repo, action.target, action.reason)

            elif action.kind is CascadeKind.CLOSE_PR:
                await github.add_issue_comment(
                    owner, repo, action.target, f"Closing: {action.reason}"
                )
                await github.update_issue(owner, repo, action.target, state="closed")

            report.applied.append(action)
        except GitHubAPIError as exc:
            logger.warning("Cascade action %s failed: %s", action, exc)
            report.errors.append(f"{action.kind.value} #{action.target}: {exc}")

    return report
