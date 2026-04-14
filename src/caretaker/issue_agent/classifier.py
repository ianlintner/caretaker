"""Issue classification logic."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caretaker.config import IssueAgentConfig
    from caretaker.github_client.models import Issue

logger = logging.getLogger(__name__)


class IssueClassification(StrEnum):
    BUG_SIMPLE = "BUG_SIMPLE"
    BUG_COMPLEX = "BUG_COMPLEX"
    FEATURE_SMALL = "FEATURE_SMALL"
    FEATURE_LARGE = "FEATURE_LARGE"
    QUESTION = "QUESTION"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    INFRA_OR_CONFIG = "INFRA_OR_CONFIG"
    MAINTAINER_INTERNAL = "MAINTAINER_INTERNAL"


def classify_issue(issue: Issue, config: IssueAgentConfig) -> IssueClassification:
    """Classify an issue based on labels, title, and body."""
    # Internal issues skip triage
    if issue.is_maintainer_issue:
        return IssueClassification.MAINTAINER_INTERNAL

    # Label-based classification
    label_names = {lbl.name.lower() for lbl in issue.labels}

    if "duplicate" in label_names:
        return IssueClassification.DUPLICATE

    if label_names & {lbl.lower() for lbl in config.labels.question}:
        return IssueClassification.QUESTION

    body_lower = (issue.body or "").lower()
    title_lower = issue.title.lower()
    text = f"{title_lower} {body_lower}"

    if "duplicate of #" in text or re.search(r"\bdup(?:licate)?\b.*#\d+", text):
        return IssueClassification.DUPLICATE

    last_activity = issue.updated_at or issue.created_at
    if last_activity is not None:
        now = datetime.now(UTC)
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        age_days = (now - last_activity).days
        if age_days >= config.auto_close_stale_days:
            return IssueClassification.STALE

    # Infrastructure / config issues
    if any(kw in text for kw in ["secret", "permission", "deploy", "ci/cd", "workflow", "token"]):
        return IssueClassification.INFRA_OR_CONFIG

    # Bug detection
    is_bug: bool = bool(label_names & {lbl.lower() for lbl in config.labels.bug})
    if not is_bug:
        is_bug = any(
            kw in text
            for kw in [
                "bug",
                "crash",
                "error",
                "exception",
                "broken",
                "not working",
                "regression",
                "fails",
                "failure",
            ]
        )

    if is_bug:
        # Simple vs complex heuristic: body length and number of files mentioned
        file_refs = len(re.findall(r"\b\w+\.\w{1,4}\b", body_lower))
        if len(body_lower) > 2000 or file_refs > 5:
            return IssueClassification.BUG_COMPLEX
        return IssueClassification.BUG_SIMPLE

    # Feature detection
    is_feature: bool = bool(label_names & {lbl.lower() for lbl in config.labels.feature})
    if not is_feature:
        is_feature = any(
            kw in text
            for kw in [
                "feature",
                "enhancement",
                "request",
                "add support",
                "implement",
                "would be nice",
                "proposal",
            ]
        )

    if is_feature:
        if len(body_lower) > 3000:
            return IssueClassification.FEATURE_LARGE
        return IssueClassification.FEATURE_SMALL

    # Question detection
    if "?" in issue.title or any(
        kw in text
        for kw in [
            "how to",
            "how do",
            "is it possible",
            "can i",
            "question",
            "help",
            "documentation",
        ]
    ):
        return IssueClassification.QUESTION

    # Default to feature small
    return IssueClassification.FEATURE_SMALL
