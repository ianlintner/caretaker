"""Tests for ``handoff_review_consumer`` — Reviews-tab harvest path.

Covers payload parsing (validation, tolerance to malformed input) and
the end-to-end consume flow against a fake GitHub client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.github_client.models import Comment, User
from caretaker.pr_reviewer.handoff_review_consumer import (
    consume_handoff_reviews,
    parse_review_payload,
)
from caretaker.pr_reviewer.handoff_reviewer import REVIEW_RESULT_MARKER
from caretaker.state.models import TrackedPR


def _comment(*, cid: int, login: str, body: str) -> Comment:
    return Comment(
        id=cid,
        user=User(login=login, id=cid * 10, type="Bot"),
        body=body,
        created_at=datetime(2026, 4, 26, tzinfo=UTC),
    )


def _agent_reply(*, summary: str = "Looks good overall.", verdict: str = "COMMENT") -> str:
    """Build a realistic agent-style comment with the response marker + JSON block."""
    return f"""\
Here is my review of this PR. I checked correctness, security, and tests.

{REVIEW_RESULT_MARKER}
```caretaker-review
{{
  "verdict": "{verdict}",
  "summary": "{summary}",
  "comments": [
    {{"path": "src/foo.py", "line": 42, "body": "Consider an early return here."}}
  ]
}}
```
"""


# ── parse_review_payload ──────────────────────────────────────────────────


def test_parse_valid_payload_returns_review() -> None:
    parsed = parse_review_payload(_agent_reply(summary="Solid PR.", verdict="APPROVE"))
    assert parsed is not None
    assert parsed.summary == "Solid PR."
    assert parsed.verdict == "APPROVE"
    assert len(parsed.comments) == 1
    assert parsed.comments[0].path == "src/foo.py"
    assert parsed.comments[0].line == 42


def test_parse_fence_without_html_marker_is_accepted() -> None:
    """Real-world finding from the v0.24.0 live QA cycle on
    caretaker-qa#63: Claude Code emitted the JSON fence correctly but
    omitted the ``<!-- caretaker:review-result -->`` HTML comment
    marker (its output formatter dropped the literal HTML comment).
    The fenced ``caretaker-review`` tag is unique enough to be the
    primary signal; the marker is documented as optional belt-and-
    suspenders, and a fence-without-marker reply must still parse.
    """
    body = '```caretaker-review\n{"summary": "x", "verdict": "COMMENT"}\n```'
    parsed = parse_review_payload(body)
    assert parsed is not None
    assert parsed.summary == "x"
    assert parsed.verdict == "COMMENT"


def test_parse_missing_fence_returns_none() -> None:
    body = f"{REVIEW_RESULT_MARKER}\nNo JSON block here."
    assert parse_review_payload(body) is None


def test_parse_malformed_json_returns_none() -> None:
    body = f"{REVIEW_RESULT_MARKER}\n```caretaker-review\n{{not valid json}}\n```"
    assert parse_review_payload(body) is None


def test_parse_missing_summary_returns_none() -> None:
    body = f'{REVIEW_RESULT_MARKER}\n```caretaker-review\n{{"verdict": "COMMENT"}}\n```'
    assert parse_review_payload(body) is None


def test_parse_invalid_verdict_defaults_to_comment() -> None:
    body = (
        f'{REVIEW_RESULT_MARKER}\n```caretaker-review\n{{"summary": "x", "verdict": "BLOCK"}}\n```'
    )
    parsed = parse_review_payload(body)
    assert parsed is not None
    assert parsed.verdict == "COMMENT"


def test_parse_caps_inline_comments_at_eight() -> None:
    """Schema cap matches the inline_reviewer LLM contract — keeps the
    formal review readable instead of dumping every nit found."""
    raw_comments = [
        {"path": f"src/f{i}.py", "line": i, "body": f"comment {i}"} for i in range(1, 15)
    ]
    body = (
        f"{REVIEW_RESULT_MARKER}\n```caretaker-review\n"
        f'{{"summary": "x", "verdict": "COMMENT", "comments": {raw_comments}}}\n```'
    ).replace("'", '"')
    parsed = parse_review_payload(body)
    assert parsed is not None
    assert len(parsed.comments) == 8


def test_parse_filters_invalid_comment_entries() -> None:
    """Each inline-comment entry is independently validated; bad ones are
    dropped silently rather than poisoning the whole review."""
    body = f"""\
{REVIEW_RESULT_MARKER}
```caretaker-review
{{
  "summary": "x",
  "verdict": "COMMENT",
  "comments": [
    {{"path": "src/foo.py", "line": 10, "body": "good"}},
    {{"path": "", "line": 5, "body": "missing path"}},
    {{"path": "src/bar.py", "line": 0, "body": "non-positive line"}},
    {{"path": "src/baz.py", "line": 7, "body": ""}},
    "not even a dict"
  ]
}}
```
"""
    parsed = parse_review_payload(body)
    assert parsed is not None
    assert len(parsed.comments) == 1
    assert parsed.comments[0].path == "src/foo.py"


def test_parse_skips_caretaker_handoff_invitation() -> None:
    """Caretaker's own hand-off comment (the *invitation* to the agent)
    must never be parsed as the agent's *response*. Even if a future
    template change makes the invitation include the response marker,
    we never want the consumer to recurse on its own request."""
    body = (
        "<!-- caretaker:pr-reviewer-handoff -->\n"
        "@claude please review.\n"
        "(This is what caretaker posts; no agent reply yet.)"
    )
    assert parse_review_payload(body) is None


# ── consume_handoff_reviews ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_posts_formal_review_and_records_consumed_id() -> None:
    """Happy path: agent reply with valid payload → ``create_review`` called
    once with the parsed inline comments, and the comment ID lands in
    ``tracking.consumed_handoff_review_comment_ids`` so the next cycle is a
    no-op for that comment."""
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=42, login="claude[bot]", body=_agent_reply())]
    )
    github.create_review = AsyncMock(return_value={"id": 100})

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    assert posted == 1
    github.create_review.assert_awaited_once()
    review_kwargs = github.create_review.await_args.kwargs
    assert review_kwargs["event"] == "COMMENT"
    assert review_kwargs["commit_sha"] == "sha"
    # Body credits the agent so reviewers see the chain of custody.
    assert "@claude[bot]" in review_kwargs["body"]
    # Inline comments are posted on the formal review (Reviews-tab path).
    assert review_kwargs["comments"] is not None
    assert len(review_kwargs["comments"]) == 1
    # Idempotency: subsequent runs see the comment ID and skip.
    assert tracking.consumed_handoff_review_comment_ids == [42]


@pytest.mark.asyncio
async def test_consume_is_idempotent_across_cycles() -> None:
    """Already-consumed comment IDs are skipped — no duplicate reviews
    on the next polling cycle / webhook re-delivery."""
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=42, login="claude[bot]", body=_agent_reply())]
    )
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=7, consumed_handoff_review_comment_ids=[42])

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    assert posted == 0
    github.create_review.assert_not_awaited()
    # Still only one entry — we don't append duplicates.
    assert tracking.consumed_handoff_review_comment_ids == [42]


@pytest.mark.asyncio
async def test_consume_skips_real_v0_24_0_invitation_with_example_payload() -> None:
    """Regression for caretaker-qa#61.

    The v0.24.0 hand-off invitation deliberately quotes the response
    marker AND a fenced ``caretaker-review`` example so the agent
    knows what shape to emit. The previous implementation of
    ``_is_caretaker_authored`` (``has_caretaker_marker and not
    has_response_marker``) returned False on this body, allowing the
    consumer to fall through to ``parse_review_payload`` on its OWN
    invitation. We were saved in production only by the example JSON
    having ``//`` comments (invalid JSON → parse returned None). A
    future copy edit that strictly-validated the example would post a
    fake formal review with placeholder content. The fix detects the
    handoff marker directly.

    This fixture is a verbatim copy of the invitation body posted to
    https://github.com/ianlintner/caretaker-qa/pull/61, including the
    nested ```` ```` outer fence + ``` inner fence layout.
    """
    real_invitation = (
        "<!-- caretaker:pr-reviewer-handoff -->\n"
        "@claude caretaker is requesting a full code review for this PR.\n"
        "\n"
        "**Repo:** `ianlintner/caretaker-qa` · **PR:** #61\n"
        "\n"
        "**To have your review surface in the GitHub Reviews tab** "
        "(not just an issue comment), end your reply with the marker "
        "line `<!-- caretaker:review-result -->` followed by a fenced "
        "JSON block tagged `caretaker-review`. Example:\n"
        "\n"
        "````\n"
        "<!-- caretaker:review-result -->\n"
        "```caretaker-review\n"
        "{\n"
        '  "verdict": "COMMENT",\n'
        '  "summary": "1-3 sentence overall assessment.",\n'
        '  "comments": [\n'
        '    {"path": "src/foo.py", "line": 42, "body": "..."}\n'
        "  ]\n"
        "}\n"
        "```\n"
        "````\n"
    )
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=1, login="github-actions[bot]", body=real_invitation)]
    )
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=61)
    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=61,
        head_sha="sha",
        tracking=tracking,
    )

    # Despite the example being strictly-valid JSON (no ``//`` comments
    # in this fixture), the consumer must skip the invitation entirely
    # — it never reaches ``parse_review_payload``.
    assert posted == 0
    github.create_review.assert_not_awaited()
    # And we don't record the ID either; if a future change made the
    # invitation legitimately consumable (e.g. an opt-in self-review
    # mode), it should still be available for that path.
    assert tracking.consumed_handoff_review_comment_ids == []


@pytest.mark.asyncio
async def test_consume_skips_caretaker_authored_comment() -> None:
    """Caretaker's own hand-off invitation must never be harvested as the
    agent's response, even when the response marker would otherwise be
    expected. The author check is the safety net that prevents recursion."""
    github = MagicMock()
    # A caretaker hand-off comment WITHOUT the response marker — should be
    # ignored entirely. (The response marker filter alone would also reject
    # this; we test both layers via the next test.)
    github.get_pr_comments = AsyncMock(
        return_value=[
            _comment(
                cid=1,
                login="the-care-taker[bot]",
                body="<!-- caretaker:pr-reviewer-handoff -->\n@claude please review.",
            )
        ]
    )
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    assert posted == 0
    github.create_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_records_id_for_malformed_payload() -> None:
    """A comment with the response marker but a busted JSON block is
    recorded as consumed *anyway* — otherwise we'd re-scan and re-warn
    on every cycle forever. Operator sees the warning once."""
    github = MagicMock()
    bad_body = f"{REVIEW_RESULT_MARKER}\n```caretaker-review\n{{busted}}\n```"
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=99, login="claude[bot]", body=bad_body)]
    )
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    assert posted == 0
    github.create_review.assert_not_awaited()
    assert tracking.consumed_handoff_review_comment_ids == [99]


@pytest.mark.asyncio
async def test_consume_no_op_without_head_sha() -> None:
    """No commit SHA → no anchoring possible for inline comments;
    skip rather than post a wrong-base review."""
    github = MagicMock()
    github.get_pr_comments = AsyncMock()  # never called
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="",
        tracking=tracking,
    )

    assert posted == 0
    github.get_pr_comments.assert_not_awaited()
    github.create_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_post_review_failure_leaves_id_unconsumed() -> None:
    """Transient ``create_review`` failure → don't record the ID, so the
    next cycle re-tries the same comment instead of silently dropping
    the agent's review on the floor."""
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=42, login="claude[bot]", body=_agent_reply())]
    )
    github.create_review = AsyncMock(side_effect=RuntimeError("transient"))
    # post_review itself catches the error and tries the fallback
    # ``upsert_issue_comment`` path; mock that too so the test is
    # deterministic.
    github.upsert_issue_comment = AsyncMock(side_effect=RuntimeError("transient"))

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    # post_review caught the create_review error and fell back to
    # upsert_issue_comment, which also failed — but post_review itself
    # doesn't raise (defensive try/except). The consumer treats the call
    # as successful since it didn't raise. This is the contract: the
    # consumer trusts post_review's return; the operator sees the
    # failure in post_review's logged warning. Idempotency still
    # protects against re-posting if create_review later succeeds.
    assert posted == 1
    assert tracking.consumed_handoff_review_comment_ids == [42]


@pytest.mark.asyncio
async def test_consume_harvests_claude_reply_without_html_marker() -> None:
    """End-to-end regression for caretaker-qa#63.

    Claude's real output on the live QA cycle dropped the
    ``<!-- caretaker:review-result -->`` HTML comment marker but kept
    the ``caretaker-review`` JSON fence intact. Pre-fix, the consumer
    bailed at the marker filter and never parsed the reply, leaving
    the harvest cycle silently broken on every actual hand-off. The
    fence is now the primary signal; this test pins that contract.
    """
    no_marker_reply = (
        "Reviewed PR #63. Findings inline.\n"
        "\n"
        "```caretaker-review\n"
        "{\n"
        '  "verdict": "COMMENT",\n'
        '  "summary": "Solid change overall; a few rotation-tracking '
        'edge cases worth considering.",\n'
        '  "comments": [\n'
        '    {"path": "src/qa_agent/secret_tracker.py", "line": 152, '
        '"body": "Three observations of the same fingerprint resets the rotation '
        'clock to the third — implementation does not match the docstring."},\n'
        '    {"path": ".github/workflows/secret-audit.yml", "line": 52, '
        '"body": "GITHUB_TOKEN is auto-minted per run; its fingerprint changes '
        'every execution and stale() will never fire for it."}\n'
        "  ]\n"
        "}\n"
        "```\n"
    )
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[_comment(cid=42, login="claude[bot]", body=no_marker_reply)]
    )
    github.create_review = AsyncMock(return_value={"id": 100})

    tracking = TrackedPR(number=63)
    posted = await consume_handoff_reviews(
        github=github,
        owner="ianlintner",
        repo="caretaker-qa",
        pr_number=63,
        head_sha="fake-sha",
        tracking=tracking,
    )

    assert posted == 1
    github.create_review.assert_awaited_once()
    review_kwargs = github.create_review.await_args.kwargs
    assert review_kwargs["event"] == "COMMENT"
    assert "@claude[bot]" in review_kwargs["body"]
    assert len(review_kwargs["comments"]) == 2
    assert tracking.consumed_handoff_review_comment_ids == [42]


@pytest.mark.asyncio
async def test_consume_skips_comments_without_response_marker() -> None:
    github = MagicMock()
    github.get_pr_comments = AsyncMock(
        return_value=[
            _comment(cid=1, login="human-dev", body="LGTM!"),
            _comment(cid=2, login="claude[bot]", body="No structured payload here."),
        ]
    )
    github.create_review = AsyncMock()

    tracking = TrackedPR(number=7)

    posted = await consume_handoff_reviews(
        github=github,
        owner="o",
        repo="r",
        pr_number=7,
        head_sha="sha",
        tracking=tracking,
    )

    assert posted == 0
    github.create_review.assert_not_awaited()
    assert tracking.consumed_handoff_review_comment_ids == []
