"""Greptile (https://greptile.com) local-subprocess stub.

Greptile is a hosted code-review service backed by a whole-repo graph
index. Integration would call its REST API (``POST /v2/review``) with
the PR diff and a repo identifier, then map the response into a
:class:`ReviewResult`. This stub documents the contract; the actual
HTTP client is intentionally unwritten so operators have a clear signal
to implement it when first opting in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from caretaker.pr_reviewer.handoff_reviewer import (
    GREPTILE_REVIEW_MARKER,
    HandoffReviewerSpec,
)

if TYPE_CHECKING:
    from caretaker.pr_reviewer.inline_reviewer import ReviewResult


async def run(*, pr_url: str, **_: object) -> ReviewResult:
    """STUB. Invoke Greptile's review API and shape the response.

    Required env: ``GREPTILE_API_KEY``. Endpoint:
    ``POST https://api.greptile.com/v2/review`` with a JSON body
    containing ``repository``, ``pull_request_number``, and
    ``ref`` (head SHA). The response includes ``summary`` and
    ``comments[]`` (path/line/body) which map cleanly onto
    :class:`ReviewResult`.

    Until implemented, dispatch to this backend raises so a
    misconfiguration is loud rather than silently dropping reviews.
    """
    raise NotImplementedError(
        f"greptile backend stubbed; provide GREPTILE_API_KEY and implement "
        f"run() in caretaker/pr_reviewer/backends/greptile.py (pr_url={pr_url!r})"
    )


SPEC = HandoffReviewerSpec(
    backend="greptile",
    marker=GREPTILE_REVIEW_MARKER,
    upstream_action_name="Greptile API (local subprocess)",
    label_color="9333ea",
    label_description="greptile review (pluggable backend)",
    invocation="local_subprocess",
    runner=run,
)


__all__ = ["SPEC", "run"]
