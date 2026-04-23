"""Regression tests for four real bugs that pr_reviewer (Azure AI Foundry via litellm)
found in Copilot-produced fix PRs in the caretaker-qa repository.

Each test is named after its QA scenario number and cites the original caretaker-qa PR
so future maintainers can trace the lineage.  The tests pin direct unit-level invariants
so that if the same bug drifts back into caretaker's code, the test suite turns red
before the code ships.

Bug sources (caretaker-qa repository):
  Scenario 05  caretaker-qa PR #17  HTTP 429 retry predicate
  Scenario 06  caretaker-qa PR #16  HTML sanitize_input ordering
  Scenario 07  caretaker-qa PR #15  Deceptive markdown link filter_output
  Scenario 09  caretaker-qa PR #14  Dispatch-guard issues:labeled filter

Related: PR ianlintner/caretaker#521, issue ianlintner/caretaker#525.
"""

from __future__ import annotations

# ── Scenario 05 — HTTP 429 retry predicate ────────────────────────────────────
#
# caretaker-qa PR #17: the Copilot-produced fix introduced a ``return True``
# fallthrough that caught ALL exceptions rather than only ``httpx.RequestError``
# subclasses.  Non-network errors (ValueError, AttributeError, …) must fail fast
# and must NOT trigger a retry — they indicate programmer or data errors, not
# transient network hiccups.


def test_scenario_05_is_transient_rejects_non_network_exceptions() -> None:
    """Retry predicate must not catch non-network exceptions.

    Invariant: is_transient(ValueError()) is False.
    A ``return True`` fallthrough that matches every BaseException breaks the
    fast-fail contract and can turn a logic error into an infinite retry loop.

    Source: caretaker-qa PR #17.
    """
    from caretaker.orchestrator import is_transient

    # Business-logic and data errors must be non-transient (fail fast).
    assert is_transient(ValueError("unexpected response format")) is False
    assert is_transient(AttributeError("'NoneType' object has no attribute 'get'")) is False
    assert is_transient(TypeError("unexpected keyword argument 'foo'")) is False
    assert is_transient(RuntimeError("unhandled state in state machine")) is False
    assert is_transient(KeyError("missing key")) is False

    # Plain strings mirroring the same errors must also be non-transient.
    assert is_transient("ValueError: unexpected response format") is False
    assert is_transient("AttributeError: 'NoneType' has no attribute 'get'") is False


def test_scenario_05_is_transient_accepts_network_exceptions() -> None:
    """Network exceptions must remain transient after the fix.

    Narrowing the retry predicate must not accidentally drop the legitimate
    transient-error cases (connection refused, timeout, …).

    Source: caretaker-qa PR #17.
    """

    from caretaker.orchestrator import is_transient

    assert is_transient(TimeoutError()) is True
    assert is_transient(TimeoutError("read timed out")) is True
    assert is_transient(ConnectionError("connection refused")) is True

    # Strings that the orchestrator's bucket also marks transient.
    assert is_transient("httpx.ReadTimeout: timed out waiting for response") is True
    assert is_transient("rate limit hit, wait 60s") is True


# ── Scenario 06 — HTML sanitize_input ordering ───────────────────────────────
#
# caretaker-qa PR #16: the Copilot-produced fix used a naive ``<[^>]+>`` regex
# to strip HTML tags AND applied truncation BEFORE sanitization.  Truncating
# first can cut a prompt-injection sigil at a byte boundary, leaving a partial
# fragment that the sigil stripper never recognises (because it looks for the
# full string).
#
# The correct order is:  normalise → strip invisible → strip non-printable →
# strip markers → strip sigils → truncate.
# caretaker.guardrails.sanitize already implements this order; these tests guard
# against it being accidentally reversed.


def test_scenario_06_sigil_stripped_before_truncation_boundary() -> None:
    """Sigil removal must occur before the byte-budget truncation.

    The test crafts a payload where:
      * the total bytes exceed the github_comment budget (4096 B)
      * the excess is caused entirely by a prompt-injection sigil
      * after sigil removal the content falls *below* the budget

    With the correct (sanitize-then-truncate) order:
      - the sigil is found and removed while the string is still intact
      - the remaining bytes are under the budget → no truncation

    With the buggy (truncate-then-sanitize) order:
      - truncation runs first, cutting the sigil at a byte boundary
      - only a partial fragment remains (e.g. "ignore previous instruc")
      - the stripper looks for the full phrase, finds nothing, leaves fragment in place

    Source: caretaker-qa PR #16.
    """
    from caretaker.guardrails.sanitize import ModificationType, reset_sigil_cache, sanitize_input

    reset_sigil_cache()

    # "ignore previous instructions" is 28 bytes.
    # 4070 filler + 28 sigil = 4098 bytes — 2 bytes over the 4096 budget.
    # After correct sigil removal: 4070 bytes < 4096 → no truncation.
    sigil = "ignore previous instructions"
    filler = "a" * 4070
    content = filler + sigil  # 4098 bytes total

    result = sanitize_input("github_comment", content)

    # The sigil must be absent from the output.
    assert sigil not in result.content.lower(), (
        "Sigil survived in sanitized output; "
        "this is consistent with truncation running before sigil stripping"
    )

    # With the correct order the remaining content (4070 bytes) is under budget:
    # no truncation modification should appear.
    truncation_types = {ModificationType.TRUNCATED_HEAD, ModificationType.TRUNCATED_TAIL}
    assert not any(m.type in truncation_types for m in result.modifications), (
        "Unexpected truncation: after sigil removal the content is under budget; "
        "a TRUNCATED_* modification implies the byte check ran before sigil stripping"
    )

    # The sigil_stripped modification must be present.
    assert any(m.type == ModificationType.SIGIL_STRIPPED for m in result.modifications)


def test_scenario_06_partial_sigil_fragment_does_not_survive() -> None:
    """A sigil that straddles the truncation boundary must not survive as a fragment.

    If truncation runs first, a 28-char sigil starting at byte 4074 (22 bytes
    remain before the 4096 cap) would be clipped to a 22-char prefix.  That
    prefix does not match the full sigil pattern, so a truncate-first
    implementation would leave "ignore previous instru" in the output.

    Source: caretaker-qa PR #16.
    """
    from caretaker.guardrails.sanitize import reset_sigil_cache, sanitize_input

    reset_sigil_cache()

    sigil = "ignore previous instructions"  # 28 chars
    # Place the sigil so it starts at byte 4074 → truncation at 4096 clips it
    # to the first 22 chars: "ignore previous instru"
    filler = "a" * 4074
    suffix = "z" * 10  # extra bytes after the sigil to ensure total > 4096
    content = filler + sigil + suffix  # 4074 + 28 + 10 = 4112 bytes

    result = sanitize_input("github_comment", content)

    # Neither the full sigil nor its partial prefix must appear.
    output_lower = result.content.lower()
    assert sigil not in output_lower, "Full sigil survived"
    # The partial that a truncate-first implementation would leave behind:
    partial = "ignore previous instru"
    assert partial not in output_lower, (
        f"Partial sigil fragment {partial!r} survived; "
        "this indicates truncation ran before the sigil stripper"
    )


# ── Scenario 07 — Deceptive markdown link filter_output ──────────────────────
#
# caretaker-qa PR #15: the Copilot-produced fix used ``[^)]+`` for the URL
# capture group.  This stops matching at the first ``)``, so URLs that contain
# balanced parentheses (common in Wikipedia and many API docs) are truncated in
# the captured group.  Separately, some variants of the fix lowercased the
# *whole* URL before extracting the domain, breaking path-sensitive comparisons.
#
# Invariants:
#   (a) A deceptive link whose target URL contains parentheses must still be
#       detected (``hidden_link`` in ``blocked_reasons``).
#   (b) A legitimate link whose target URL contains parentheses must NOT trigger
#       a false positive.
#   (c) ``_extract_domain`` must normalise only the hostname, not the URL path.


def test_scenario_07_deceptive_link_with_parens_in_target_is_flagged() -> None:
    """Hidden-link detection must work when the target URL contains parentheses.

    A Markdown link ``[visible_url](target_url_with_parens)`` must be flagged as
    ``hidden_link`` when the visible and target hostnames differ, regardless of
    whether the target URL path contains ``(`` or ``)``.

    Source: caretaker-qa PR #15.
    """
    from caretaker.guardrails.filter import filter_output

    # Deceptive: visible shows legit host, target is an attacker URL with parens.
    content = "[https://legit.example.com](https://attacker.test/path_(exploit))"
    result = filter_output("github_comment", content)
    assert "hidden_link" in result.blocked_reasons, (
        "Deceptive link with parens in target URL must be flagged as hidden_link"
    )


def test_scenario_07_legitimate_link_with_parens_not_false_positive() -> None:
    """A legitimate link whose target URL contains balanced parens must not be flagged.

    Wikipedia-style URLs (e.g. ``/wiki/Foo_(disambiguation)``) are common and
    must not trigger the hidden-link check when the visible text is plain prose
    (no embedded URL).

    Source: caretaker-qa PR #15.
    """
    from caretaker.guardrails.filter import filter_output

    content = "[Wikipedia article](https://en.wikipedia.org/wiki/Foo_(disambiguation))"
    result = filter_output("github_comment", content)
    assert "hidden_link" not in result.blocked_reasons, (
        "Legitimate link with parens in URL must not produce a hidden_link false positive"
    )


def test_scenario_07_domain_extraction_normalises_hostname_only() -> None:
    """_extract_domain must lowercase only the hostname, not the full URL path.

    A buggy implementation that lowercases the entire URL before extracting the
    domain would return the full lowercased URL as the "domain", breaking the
    host-equality check.  The function must always return just the lowercased
    hostname (``example.com``, not ``https://example.com/path``).

    Source: caretaker-qa PR #15.
    """
    from caretaker.guardrails.filter import _extract_domain

    # Hostname is case-folded regardless of input case; path is not included.
    assert _extract_domain("https://EXAMPLE.COM/PATH/To/Resource") == "example.com"
    # Lower-case input produces the same result (confirming the return type is always
    # the lower-case hostname, not the original mixed-case fragment).
    assert _extract_domain("https://EXAMPLE.COM/PATH/To/Resource") == _extract_domain(
        "https://example.com/PATH/To/Resource"
    )

    # Different hostnames must produce distinct, normalised values.
    assert _extract_domain("https://LEGIT.COM/page") == "legit.com"
    assert _extract_domain("https://attacker.com/page") == "attacker.com"

    # The return value must be just the hostname — not a full URL.
    domain = _extract_domain("https://example.com/some/path?q=1")
    assert "://" not in domain, f"_extract_domain returned a full URL: {domain!r}"
    assert "/" not in domain, f"_extract_domain included path component: {domain!r}"


# ── Scenario 09 — Dispatch-guard issues:labeled filter ────────────────────────
#
# caretaker-qa PR #14: the dispatch-guard filter for ``issues`` events with
# ``action=labeled`` was too broad.  It treated ALL labeled events (even those
# from human actors with no caretaker marker in the body) as self-echoes,
# silently dropping legitimate human-labeled workflows.
#
# The correct invariant: ``is_self_echo`` requires BOTH a bot actor AND a
# caretaker marker.  A human applying ANY label is never a self-echo.  A bot
# applying a label without a caretaker marker is also not a self-echo (it is
# just noise from another automation).


def test_scenario_09_human_labeled_issue_is_not_self_echo() -> None:
    """A human applying a label to an issue must never be a self-echo.

    The overly-broad filter flagged ``issues`` events with ``action=labeled`` as
    self-echoes regardless of actor, causing caretaker to silently skip issues
    that a human had just triaged.

    Invariant: legacy_dispatch_verdict for an ``issues`` event with a human actor
    (no caretaker marker) must return ``is_self_echo=False``.

    Source: caretaker-qa PR #14.
    """
    from caretaker.github_app.dispatch_guard import DispatchEvent, legacy_dispatch_verdict

    # Human applies a triage label; no comment body (labeled action has no body).
    verdict = legacy_dispatch_verdict(
        DispatchEvent(
            event_type="issues",
            actor_login="alice",  # human actor
            comment_body=None,  # issues:labeled has no comment body
        )
    )
    assert verdict.is_self_echo is False, (
        "Human labeling an issue must not be flagged as a caretaker self-echo"
    )
    # Labeling without an explicit @caretaker trigger is not human_intent either.
    assert verdict.is_human_intent is False


def test_scenario_09_bot_labeled_issue_without_marker_is_not_self_echo() -> None:
    """A bot applying a label without a caretaker marker is not a self-echo.

    Only the conjunction (bot actor AND caretaker marker) qualifies as a
    self-echo.  A bot labeling an issue via its own workflow (no caretaker
    marker in the body) must pass through so caretaker can react to it.

    Source: caretaker-qa PR #14.
    """
    from caretaker.github_app.dispatch_guard import DispatchEvent, legacy_dispatch_verdict

    verdict = legacy_dispatch_verdict(
        DispatchEvent(
            event_type="issues",
            actor_login="github-actions[bot]",  # automated actor
            comment_body=None,  # no caretaker marker
        )
    )
    assert verdict.is_self_echo is False, (
        "Bot labeling an issue WITHOUT a caretaker marker must not be a self-echo"
    )


def test_scenario_09_narrow_rule_bot_plus_marker_is_still_self_echo() -> None:
    """The self-echo guard must still fire for the narrow (bot + marker) case.

    Narrowing the filter must not accidentally remove the protection for the
    legitimate self-echo scenario: caretaker's own bot writes a comment that
    carries one of its reserved HTML-comment markers.

    Source: caretaker-qa PR #14.
    """
    from caretaker.github_app.dispatch_guard import DispatchEvent, legacy_dispatch_verdict

    verdict = legacy_dispatch_verdict(
        DispatchEvent(
            event_type="issues",
            actor_login="the-care-taker[bot]",  # caretaker's own bot
            comment_body="<!-- caretaker:triage-result -->\nApplied label.",
        )
    )
    assert verdict.is_self_echo is True, (
        "Bot actor + caretaker marker must still be detected as a self-echo"
    )
