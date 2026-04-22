"""GitHub PR opener for fix-ladder ``fixed`` / ``partial`` outcomes.

Split out of :mod:`caretaker.self_heal_agent.fix_ladder` so the
runner stays pure (sandbox + diff production) and this module owns
the network side-effects. Kept intentionally thin — we use the
contents API one file at a time rather than the Git Data API
tree/blob dance because fix-ladder diffs are tiny (lint autofix,
formatter reshuffle) and the readability win beats the extra API
round-trips.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient
    from caretaker.self_heal_agent.fix_ladder import FixLadderResult

logger = logging.getLogger(__name__)


@dataclass
class FixLadderPROpen:
    """Summary of a successfully opened fix-ladder PR."""

    number: int
    html_url: str | None
    branch: str


# Match the file path on either side of a ``diff --git`` header.
# Accepts paths with or without the ``a/`` / ``b/`` prefixes that
# ``git diff`` normally emits — some rungs (pytest cache, pip-compile
# output) produce diff-like output with different prefixes.
_DIFF_HEADER_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def _changed_files_from_diff(patch: str) -> list[str]:
    """Extract the repo-relative paths touched by ``patch``.

    Uses the ``b/<path>`` side (post-image) because that's the path
    that exists on disk after the rung runs — the side we need to
    read and upload. Returns a de-duplicated list preserving the
    order paths first appear in the diff.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _DIFF_HEADER_PATTERN.finditer(patch):
        path = match.group(2)
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def _short_signature(sig: str) -> str:
    """Return a branch-safe prefix of ``sig`` (≤ 12 chars, alnum)."""
    trimmed = re.sub(r"[^A-Za-z0-9]", "", sig)[:12]
    return trimmed or "unknown"


async def open_fix_ladder_pr(
    *,
    github: GitHubClient,
    owner: str,
    repo: str,
    base_branch: str,
    sandbox_root: Path | str,
    result: FixLadderResult,
    error_signature: str,
    branch_prefix: str = "caretaker/fix-ladder",
    pr_label: str = "caretaker:fix-ladder",
) -> FixLadderPROpen | None:
    """Upload the ladder's patch as a PR against ``base_branch``.

    Returns ``None`` when the result has no patch (``outcome=no_op`` /
    ``escalated`` / ``error``) or when there are no changed files to
    commit — the caller decides whether to treat that as "nothing
    to open". All GitHub API errors surface to the caller so the
    self-heal agent can record them on the report, same as it does
    for local-issue creation failures.
    """
    patch = result.patch
    if not patch or result.outcome not in {"fixed", "partial"}:
        return None

    changed = _changed_files_from_diff(patch)
    if not changed:
        logger.info("fix_ladder: patch produced but no changed files found in diff")
        return None

    root = Path(sandbox_root)
    branch = f"{branch_prefix}/{_short_signature(error_signature)}"
    commit_message = result.commit_message or (
        f"fix(auto): fix-ladder patch for sig:{error_signature}"
    )
    title_rung = result.winning_rung or "fix-ladder"
    title = f"fix(auto): {title_rung} applied automatically"

    base_sha = await github.get_default_branch_sha(owner, repo, base_branch)
    try:
        await github.create_branch(owner, repo, branch, base_sha)
    except Exception as exc:  # noqa: BLE001 - 422 branch-exists and other errors share response shape
        # A fix-ladder PR for the same signature is already in flight
        # or was just closed. Log and bail; the outer cooldown logic
        # on the agent handles re-entry after N hours.
        logger.info(
            "fix_ladder: could not create branch %s (%s) — skipping PR open",
            branch,
            exc,
        )
        return None

    for rel_path in changed:
        absolute = root / rel_path
        if not absolute.is_file():
            logger.info("fix_ladder: skipping %s — not a regular file in sandbox", rel_path)
            continue
        try:
            content = absolute.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Fix-ladder rungs never touch binary files in practice
            # (ruff / mypy / pytest all operate on text). If we see
            # one we skip it rather than shipping a corrupted diff.
            logger.info("fix_ladder: skipping non-UTF8 file %s", rel_path)
            continue
        # The contents API wants the blob SHA when updating an
        # existing file; fetch it first so GitHub's conflict guard
        # works. Missing file → first-create (no sha).
        existing = None
        try:
            existing = await github.get_file_contents(owner, repo, rel_path, ref=branch)
        except AttributeError:
            # Older ``GitHubClient`` instances may not have the
            # helper bound yet — fall back to "no sha" and let the
            # contents endpoint create-or-update based on presence.
            existing = None
        except Exception as exc:  # noqa: BLE001 - 404 "not found" is the common case
            logger.debug("fix_ladder: get_file_contents(%s) raised (%s)", rel_path, exc)
            existing = None
        blob_sha: str | None = None
        if isinstance(existing, dict):
            sha_val = existing.get("sha")
            if isinstance(sha_val, str):
                blob_sha = sha_val
        await github.create_or_update_file(
            owner,
            repo,
            rel_path,
            message=commit_message,
            content=content,
            branch=branch,
            sha=blob_sha,
        )

    pr_body_lines = [
        "## Automatic fix applied by the self-heal fix ladder",
        "",
        f"**Error signature:** `{error_signature}`",
        "",
        "Rungs that produced this patch:",
    ]
    for record in result.rungs_run:
        if not record.produced_diff:
            continue
        pr_body_lines.append(f"- `{record.name}` — exit={record.exit_code}")
    pr_body_lines.append("")
    pr_body_lines.append(
        "See `src/caretaker/self_heal_agent/fix_ladder.py` for the rung ladder "
        "and `docs/` for the BitsAI-Fix / Factory.ai / KubeIntellect research "
        "this pattern is modelled on."
    )
    if result.outcome == "partial":
        pr_body_lines.append("")
        pr_body_lines.append(
            "> **Note:** the ladder reports `partial` — review the companion "
            "escalation issue for the remaining work."
        )
    pr_body = "\n".join(pr_body_lines)

    pr_data = await github.create_pull_request(
        owner=owner,
        repo=repo,
        title=title,
        body=pr_body,
        head=branch,
        base=base_branch,
        labels=[pr_label],
    )
    return FixLadderPROpen(
        number=int(pr_data["number"]),
        html_url=pr_data.get("html_url"),
        branch=branch,
    )


__all__ = ["FixLadderPROpen", "open_fix_ladder_pr"]
