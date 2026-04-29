"""CodeRabbit (https://coderabbit.ai) comment-trigger stub.

CodeRabbit is a hosted GitHub App that reviews PRs when mentioned with
``@coderabbitai review``. Caretaker can post the trigger comment but
the response harvest path (parsing CodeRabbit's native comment format
into the ``caretaker-review`` JSON schema) is intentionally unwritten —
adding it requires a sample of CodeRabbit's current comment shape and
should be done when an operator first opts the backend in.

This stub registers the spec so the registry shape stays generic and so
operators can see the backend in ``known_backends()``. Dispatching to
``coderabbit`` requires both:

  1. Adding ``"coderabbit"`` to ``PRReviewerConfig.enabled_backends``.
  2. Implementing the harvest parser referenced in the docstring of
     :func:`harvest_response`.
"""

from __future__ import annotations

from caretaker.pr_reviewer.handoff_reviewer import (
    CODERABBIT_REVIEW_MARKER,
    HandoffReviewerSpec,
)


def harvest_response(comment_body: str) -> None:
    """STUB. Convert a CodeRabbit comment body into a caretaker review payload.

    Implementation note for the next contributor: CodeRabbit posts its
    review as a single Markdown comment with ``## Walkthrough`` /
    ``## Changes`` / ``## Sequence Diagram(s)`` sections. The harvester
    should pull the walkthrough into ``summary`` and emit the file-level
    callouts as inline comments anchored on the file's first changed
    line (CodeRabbit doesn't expose precise line anchors in the comment
    text). Until written, this returns ``None`` and the comment-trigger
    dispatch is the only working path for this backend.
    """
    return None


SPEC = HandoffReviewerSpec(
    backend="coderabbit",
    marker=CODERABBIT_REVIEW_MARKER,
    upstream_action_name="CodeRabbit GitHub App",
    label_color="ff6b35",
    label_description="coderabbit review trigger",
    invocation="comment_trigger",
)


__all__ = ["SPEC", "harvest_response"]
