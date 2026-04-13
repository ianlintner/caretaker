"""Issue classification logic."""

from __future__ import annotations

import logging
import re
from enum import Enum

from project_maintainer.config import IssueAgentConfig
from project_maintainer.github_client.models import Issue

logger = logging.getLogger(__name__)


class IssueClassification(str, Enum):
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
    label_names = {l.name.lower() for l in issue.labels}

    if label_names & {l.lower() for l in config.labels.question}:
        return IssueClassification.QUESTION

    body_lower = (issue.body or "").lower()
    title_lower = issue.title.lower()
    text = f"{title_lower} {body_lower}"

    # Infrastructure / config issues
    if any(kw in text for kw in ["secret", "permission", "deploy", "ci/cd", "workflow", "token"]):
        return IssueClassification.INFRA_OR_CONFIG

    # Bug detection
    is_bug = label_names & {l.lower() for l in config.labels.bug}
    if not is_bug:
        is_bug = any(kw in text for kw in [
            "bug", "crash", "error", "exception", "broken", "not working",
            "regression", "fails", "failure",
        ])

    if is_bug:
        # Simple vs complex heuristic: body length and number of files mentioned
        file_refs = len(re.findall(r"\b\w+\.\w{1,4}\b", body_lower))
        if len(body_lower) > 2000 or file_refs > 5:
            return IssueClassification.BUG_COMPLEX
        return IssueClassification.BUG_SIMPLE

    # Feature detection
    is_feature = label_names & {l.lower() for l in config.labels.feature}
    if not is_feature:
        is_feature = any(kw in text for kw in [
            "feature", "enhancement", "request", "add support", "implement",
            "would be nice", "proposal",
        ])

    if is_feature:
        if len(body_lower) > 3000:
            return IssueClassification.FEATURE_LARGE
        return IssueClassification.FEATURE_SMALL

    # Question detection
    if "?" in issue.title or any(kw in text for kw in [
        "how to", "how do", "is it possible", "can i", "question",
        "help", "documentation",
    ]):
        return IssueClassification.QUESTION

    # Default to feature small
    return IssueClassification.FEATURE_SMALL
