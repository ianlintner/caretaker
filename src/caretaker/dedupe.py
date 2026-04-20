"""Shared PR dedupe helpers.

Generalisation of the upgrade-agent ``close_superseded_upgrade_prs`` pattern:
given a list of open PRs and a bucketing function, keep the newest PR per
non-empty bucket and close the rest with a superseded comment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from caretaker.github_client.api import GitHubClient
    from caretaker.github_client.models import PullRequest

logger = logging.getLogger(__name__)


async def close_superseded_prs(
    github: GitHubClient,
    owner: str,
    repo: str,
    prs: list[PullRequest],
    *,
    bucket_key: Callable[[PullRequest], str | None],
    comment: Callable[[PullRequest, PullRequest], str],
) -> list[int]:
    """Close older PRs in each bucket, keeping the newest (highest number).

    ``bucket_key(pr)`` returns a grouping key (e.g. ``"upgrade:1.4.0"``) or
    ``None`` to exclude the PR from dedupe. PRs with the same non-``None``
    key are grouped; the highest-numbered is kept, the rest are closed with
    a per-PR comment built by ``comment(closed_pr, keeper_pr)``.

    Returns the list of PR numbers that were closed.
    """
    buckets: dict[str, list[PullRequest]] = {}
    for pr in prs:
        key = bucket_key(pr)
        if key is None:
            continue
        buckets.setdefault(key, []).append(pr)

    closed: list[int] = []
    for key, group in buckets.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda p: p.number)
        *to_close, keeper = group
        for pr in to_close:
            try:
                await github.add_issue_comment(owner, repo, pr.number, comment(pr, keeper))
            except Exception as e:
                logger.warning(
                    "Failed to add superseded comment on PR #%d (bucket=%s): %s",
                    pr.number,
                    key,
                    e,
                )
            try:
                await github.update_issue(owner, repo, pr.number, state="closed")
                closed.append(pr.number)
                logger.info(
                    "Closed superseded PR #%d (kept #%d) in bucket %s",
                    pr.number,
                    keeper.number,
                    key,
                )
            except Exception as e:
                logger.warning(
                    "Failed to close superseded PR #%d (bucket=%s): %s",
                    pr.number,
                    key,
                    e,
                )
    return closed
