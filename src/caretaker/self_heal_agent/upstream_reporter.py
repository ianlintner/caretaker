"""Upstream reporter — opens bug reports / feature requests in the caretaker source repo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)

# The canonical upstream caretaker repository
UPSTREAM_OWNER = "ianlintner"
UPSTREAM_REPO = "caretaker"

UPSTREAM_BUG_MARKER = "<!-- caretaker:upstream-bug -->"
UPSTREAM_FEATURE_MARKER = "<!-- caretaker:upstream-feature -->"


@dataclass
class UpstreamReport:
    kind: str  # "bug" | "feature"
    title: str
    body: str
    issue_number: int = 0
    skipped: bool = False
    skip_reason: str = ""


async def report_upstream_bug(
    github: GitHubClient,
    title: str,
    description: str,
    context: str = "",
    caretaker_version: str = "unknown",
    reporter_repo: str = "unknown",
) -> UpstreamReport:
    """Open a bug report in the upstream caretaker repo (idempotent — checks for duplicates)."""
    report = UpstreamReport(kind="bug", title=title, body="")

    # Dedup: check if a similar open issue already exists
    existing = await _find_existing(github, "bug", title)
    if existing:
        report.skipped = True
        report.skip_reason = f"Similar issue already exists: #{existing}"
        logger.info("Upstream bug report skipped — duplicate of #%d", existing)
        return report

    body = _bug_body(title, description, context, caretaker_version, reporter_repo)
    report.body = body

    try:
        issue = await github.create_issue(
            owner=UPSTREAM_OWNER,
            repo=UPSTREAM_REPO,
            title=f"[auto] {title}",
            body=body,
            labels=["bug", "auto-reported"],
        )
        report.issue_number = issue.number
        logger.info("Opened upstream bug report: %s#%d", UPSTREAM_REPO, issue.number)
    except Exception as e:
        logger.error("Failed to open upstream bug report: %s", e)
        report.skip_reason = str(e)

    return report


async def report_upstream_feature(
    github: GitHubClient,
    title: str,
    description: str,
    use_case: str = "",
    caretaker_version: str = "unknown",
    reporter_repo: str = "unknown",
) -> UpstreamReport:
    """Open a feature request in the upstream caretaker repo (idempotent)."""
    report = UpstreamReport(kind="feature", title=title, body="")

    existing = await _find_existing(github, "enhancement", title)
    if existing:
        report.skipped = True
        report.skip_reason = f"Similar feature request already exists: #{existing}"
        logger.info("Upstream feature request skipped — duplicate of #%d", existing)
        return report

    body = _feature_body(title, description, use_case, caretaker_version, reporter_repo)
    report.body = body

    try:
        issue = await github.create_issue(
            owner=UPSTREAM_OWNER,
            repo=UPSTREAM_REPO,
            title=f"[feature] {title}",
            body=body,
            labels=["enhancement", "auto-reported"],
        )
        report.issue_number = issue.number
        logger.info("Opened upstream feature request: %s#%d", UPSTREAM_REPO, issue.number)
    except Exception as e:
        logger.error("Failed to open upstream feature request: %s", e)
        report.skip_reason = str(e)

    return report


async def _find_existing(github: GitHubClient, label: str, title: str) -> int | None:
    """Return issue number if a similar open issue already exists, else None."""
    try:
        issues = await github.list_issues(UPSTREAM_OWNER, UPSTREAM_REPO, state="open", labels=label)
        title_lower = title.lower()[:60]
        for issue in issues:
            if title_lower in issue.title.lower():
                return issue.number
    except Exception as e:
        logger.debug("Could not check upstream issues for duplicates: %s", e)
    return None


def _bug_body(
    title: str,
    description: str,
    context: str,
    version: str,
    reporter_repo: str,
) -> str:
    return f"""{UPSTREAM_BUG_MARKER}

## Bug Report (auto-reported by caretaker self-heal)

**Caretaker version:** `{version}`
**Reported from repo:** `{reporter_repo}`

### Description

{description}

### Context / logs

```
{context[:3000] if context else "N/A"}
```

### Steps to reproduce

_Auto-detected during a caretaker self-heal run. See context above._

### Expected behavior

_Caretaker should run without errors._

### Actual behavior

{title}

---
_This issue was automatically reported by the caretaker self-heal agent.
If this is a misconfiguration in the consuming repo, please close with label `not-a-bug`._
"""


def _feature_body(
    title: str,
    description: str,
    use_case: str,
    version: str,
    reporter_repo: str,
) -> str:
    return f"""{UPSTREAM_FEATURE_MARKER}

## Feature Request (auto-reported by caretaker self-heal)

**Caretaker version:** `{version}`
**Requested from repo:** `{reporter_repo}`

### Summary

{title}

### Description

{description}

### Use case

{use_case or "_See description above._"}

---
_This feature request was automatically reported by the caretaker self-heal agent._
"""
