"""Tests for the grouped-Dependabot PR bisector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from caretaker.dependency_agent.agent import DependencyAgent
from caretaker.dependency_agent.bisector import (
    BISECTOR_COMMENT_MARKER,
    PackageUpdate,
    ProbeOutcome,
    bisect_grouped_dependabot_pr,
    format_bisect_comment,
    parse_grouped_pr_body,
    synthesize_merge_plan,
)
from caretaker.github_client.models import Comment, Label, PullRequest, User

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dependency_agent"


# ────────────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────────────


class TestParseGroupedPrBody:
    def test_empty_body_returns_empty(self) -> None:
        assert parse_grouped_pr_body("") == []
        assert parse_grouped_pr_body("Some unrelated text") == []

    def test_single_header_table(self) -> None:
        body = (
            "Bumps the npm_and_yarn group with 2 updates in the /backend directory:\n\n"
            "| Package | From | To |\n"
            "| --- | --- | --- |\n"
            "| [lodash](https://github.com/lodash/lodash) | `4.17.20` | `4.17.21` |\n"
            "| minimatch | `3.1.2` | `3.1.5` |\n"
        )
        updates = parse_grouped_pr_body(body)
        assert len(updates) == 2
        assert updates[0].name == "lodash"
        assert updates[0].ecosystem == "npm"
        assert updates[0].directory == "/backend"
        assert updates[0].from_version == "4.17.20"
        assert updates[0].to_version == "4.17.21"
        assert updates[1].name == "minimatch"

    def test_multi_directory_grouped(self) -> None:
        body = (
            "Bumps the npm_and_yarn group with 1 update in the /a directory:\n\n"
            "| Package | From | To |\n"
            "| --- | --- | --- |\n"
            "| [lodash](https://example.com/lodash) | `4.17.20` | `4.17.21` |\n"
            "\n"
            "Bumps the pip group with 1 update in the /b directory:\n\n"
            "| Package | From | To |\n"
            "| --- | --- | --- |\n"
            "| [requests](https://example.com/requests) | `2.28.0` | `2.29.0` |\n"
        )
        updates = parse_grouped_pr_body(body)
        assert len(updates) == 2
        directories = {u.directory for u in updates}
        ecosystems = {u.ecosystem for u in updates}
        assert directories == {"/a", "/b"}
        assert ecosystems == {"npm", "pip"}

    def test_inline_single_update_group(self) -> None:
        body = (
            "Bumps the npm_and_yarn group with 1 update in the /embed directory: "
            "[vite](https://github.com/vitejs/vite/tree/HEAD/packages/vite).\n\n"
            "Updates `vite` from 4.5.0 to 4.5.3\n"
        )
        updates = parse_grouped_pr_body(body)
        assert len(updates) == 1
        assert updates[0].name == "vite"
        assert updates[0].ecosystem == "npm"
        assert updates[0].directory == "/embed"
        assert updates[0].from_version == "4.5.0"
        assert updates[0].to_version == "4.5.3"

    def test_dedupes_repeated_rows(self) -> None:
        # Dependabot occasionally duplicates a row between summary and
        # details sections; the parser must not count it twice.
        body = (
            "Bumps the pip group with 1 update in the / directory:\n\n"
            "| Package | From | To |\n"
            "| --- | --- | --- |\n"
            "| requests | `2.28.0` | `2.29.0` |\n"
            "\n"
            "Updates `requests` from 2.28.0 to 2.29.0\n"
        )
        updates = parse_grouped_pr_body(body)
        assert len(updates) == 1

    def test_real_dependabot_fixture(self) -> None:
        """Golden test against a real Dependabot grouped-PR body.

        Fixture is the body of Example-React-AI-Chat-App#195:
        npm_and_yarn across 4 directories, 25 updates total
        (10 /backend + 1 /embed + 12 /frontend + 2 /web).
        """
        body = (FIXTURE_DIR / "grouped_pr_195_body.md").read_text()
        updates = parse_grouped_pr_body(body)

        assert len(updates) >= 20, (
            f"parser pulled only {len(updates)} updates out of a PR listing 25"
        )
        names = {u.name for u in updates}
        # Sampling of well-known packages from the fixture.
        for expected in (
            "express-rate-limit",
            "brace-expansion",
            "minimatch",
            "undici",
            "vite",
        ):
            assert expected in names, f"missing {expected}"
        directories = {u.directory for u in updates}
        assert directories == {"/backend", "/embed", "/frontend", "/web"}
        for u in updates:
            assert u.ecosystem == "npm"


# ────────────────────────────────────────────────────────────────────
# Plan synthesizer
# ────────────────────────────────────────────────────────────────────


def _mk(name: str, *, directory: str = "/") -> PackageUpdate:
    return PackageUpdate(
        ecosystem="npm",
        name=name,
        from_version="1.0.0",
        to_version="1.0.1",
        directory=directory,
    )


class TestSynthesizeMergePlan:
    def test_all_safe(self) -> None:
        plan = synthesize_merge_plan(safe=[_mk("a"), _mk("b")], guilty=[])
        assert len(plan) == 2
        assert all(step.startswith("Merge: ") for step in plan)

    def test_guilty_produces_hold_and_followup(self) -> None:
        plan = synthesize_merge_plan(safe=[_mk("a")], guilty=[_mk("bad")])
        assert any("Merge: a" in s for s in plan)
        assert any("Hold: bad" in s for s in plan)
        assert any("Open followup issue: bad upgrade" in s for s in plan)

    def test_budget_exhausted_flags_human_review(self) -> None:
        plan = synthesize_merge_plan(
            safe=[_mk("a")],
            guilty=[],
            inconclusive=[_mk("x"), _mk("y")],
            budget_exhausted=True,
        )
        assert any("budget exhausted" in s for s in plan)


# ────────────────────────────────────────────────────────────────────
# Bisect loop
# ────────────────────────────────────────────────────────────────────


def _mk_grouped_pr(body: str, *, number: int = 1, labels: list[Label] | None = None) -> PullRequest:
    return PullRequest(
        number=number,
        title="Bump the npm_and_yarn group across 1 directory with 8 updates",
        body=body,
        state="open",
        user=User(login="dependabot[bot]", id=2),
        head_ref="dependabot/npm_and_yarn/group-8",
        base_ref="main",
        mergeable=True,
        merged=False,
        draft=False,
        labels=labels or [],
        html_url=f"https://github.com/o/r/pull/{number}",
    )


def _body_with(packages: list[str]) -> str:
    """Render a synthetic grouped-PR body for the given package names."""
    rows = "\n".join(f"| {name} | `1.0.0` | `1.0.1` |" for name in packages)
    return (
        f"Bumps the npm_and_yarn group with {len(packages)} updates in the / directory:\n\n"
        "| Package | From | To |\n"
        "| --- | --- | --- |\n"
        f"{rows}\n"
    )


class _ScriptedProbe:
    """CI probe stub: fails whenever any package in ``guilty_names`` is present."""

    def __init__(self, guilty_names: set[str]) -> None:
        self.guilty_names = guilty_names
        self.calls: list[list[str]] = []

    async def __call__(self, subset: list[PackageUpdate]) -> ProbeOutcome:
        names = [u.name for u in subset]
        self.calls.append(names)
        if any(n in self.guilty_names for n in names):
            return ProbeOutcome(passed=False, reason="simulated failure")
        return ProbeOutcome(passed=True)


class TestBisectLoop:
    @pytest.mark.asyncio
    async def test_returns_inconclusive_for_non_grouped(self) -> None:
        pr = _mk_grouped_pr("just prose, no table")
        result = await bisect_grouped_dependabot_pr(pr, github=MagicMock())
        assert result.outcome == "inconclusive"
        assert result.reason == "not_grouped"
        assert result.suggested_merge_plan == []

    @pytest.mark.asyncio
    async def test_advisory_mode_without_probe(self) -> None:
        pr = _mk_grouped_pr(_body_with(["a", "b", "c"]))
        result = await bisect_grouped_dependabot_pr(pr, github=MagicMock())
        assert result.outcome == "inconclusive"
        assert result.reason == "no_probe_configured"
        assert result.runs_consumed == 0
        assert len(result.suggested_merge_plan) >= 1
        assert any("human review" in s for s in result.suggested_merge_plan)

    @pytest.mark.asyncio
    async def test_full_bundle_green(self) -> None:
        pr = _mk_grouped_pr(_body_with(["a", "b", "c"]))
        probe = _ScriptedProbe(guilty_names=set())
        result = await bisect_grouped_dependabot_pr(pr, github=MagicMock(), ci_probe=probe)
        assert result.outcome == "all_green"
        assert len(result.safe_updates) == 3
        # One probe of the full bundle — never split.
        assert result.runs_consumed == 1

    @pytest.mark.asyncio
    async def test_converges_on_single_guilty_in_logarithmic_probes(self) -> None:
        # 8 updates, 1 guilty → expect ceil(log2(8)) + 1 ≈ 4 or fewer probes.
        names = ["a", "b", "c", "d", "e", "f", "g", "h"]
        pr = _mk_grouped_pr(_body_with(names))
        probe = _ScriptedProbe(guilty_names={"e"})

        result = await bisect_grouped_dependabot_pr(
            pr, github=MagicMock(), ci_probe=probe, max_runs=6
        )

        assert result.outcome == "guilty_identified"
        assert [u.name for u in result.guilty_updates] == ["e"]
        assert {u.name for u in result.safe_updates} == {n for n in names if n != "e"}
        # Full bundle probe + at most log2(n)=3 narrowing rounds of 2
        # probes each; we easily fit in 6.
        assert result.runs_consumed <= 6
        # And strictly fewer than a linear scan.
        assert result.runs_consumed < len(names)
        assert any("Hold: e" in s for s in result.suggested_merge_plan)
        assert any("Merge: a" in s for s in result.suggested_merge_plan)

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_partial(self) -> None:
        # 16 updates + 1 guilty + tight budget → bisect can't converge.
        names = [f"pkg{i}" for i in range(16)]
        pr = _mk_grouped_pr(_body_with(names))
        probe = _ScriptedProbe(guilty_names={"pkg9"})

        result = await bisect_grouped_dependabot_pr(
            pr, github=MagicMock(), ci_probe=probe, max_runs=2
        )

        assert result.outcome == "inconclusive"
        assert result.reason == "budget_exhausted"
        assert result.runs_consumed == 2
        # Some narrowing should still have happened: half the bundle
        # got confirmed safe.
        assert len(result.safe_updates) > 0
        assert any("budget exhausted" in step.lower() for step in result.suggested_merge_plan)

    @pytest.mark.asyncio
    async def test_handles_two_independent_culprits(self) -> None:
        names = ["a", "b", "c", "d", "e", "f", "g", "h"]
        pr = _mk_grouped_pr(_body_with(names))
        # Put one guilty in each half so both halves fail individually.
        probe = _ScriptedProbe(guilty_names={"b", "g"})

        result = await bisect_grouped_dependabot_pr(
            pr, github=MagicMock(), ci_probe=probe, max_runs=6
        )

        # With 6 runs we may not fully converge on both; we accept
        # either ``guilty_identified`` with both found, or
        # ``inconclusive`` with at least one confirmed guilty.
        assert result.outcome in {"guilty_identified", "inconclusive"}
        guilty_names = {u.name for u in result.guilty_updates}
        assert "b" in guilty_names or "g" in guilty_names


# ────────────────────────────────────────────────────────────────────
# Comment formatter
# ────────────────────────────────────────────────────────────────────


class TestFormatBisectComment:
    def test_comment_contains_marker(self) -> None:
        from caretaker.dependency_agent.bisector import BisectResult

        result = BisectResult(
            outcome="guilty_identified",
            safe_updates=[_mk("safe-pkg")],
            guilty_updates=[_mk("bad-pkg")],
            suggested_merge_plan=["Merge: safe-pkg 1.0.0->1.0.1 (/)"],
            runs_consumed=3,
        )
        body = format_bisect_comment(result)
        assert BISECTOR_COMMENT_MARKER in body
        assert "safe-pkg" in body
        assert "bad-pkg" in body
        assert "CI runs consumed:** 3" in body


# ────────────────────────────────────────────────────────────────────
# Agent integration hook
# ────────────────────────────────────────────────────────────────────


def _make_agent_github(
    prs: list[PullRequest],
    *,
    ci_status: str = "failure",
    existing_comments: list[Comment] | None = None,
) -> AsyncMock:
    from caretaker.github_client.models import Issue

    gh = AsyncMock()
    gh.list_pull_requests.return_value = prs
    gh.get_combined_status.return_value = ci_status
    gh.get_pr_comments.return_value = existing_comments or []
    gh.add_issue_comment.return_value = None
    gh.list_issues.return_value = []
    gh.create_issue.return_value = Issue(
        number=99,
        title="",
        body="",
        state="open",
        user=User(login="bot", id=1),
        labels=[],
        assignees=[],
        html_url="",
    )
    gh.ensure_label.return_value = None
    return gh


class TestAgentBisectorIntegration:
    @pytest.mark.asyncio
    async def test_fires_when_owned_and_unstable(self) -> None:
        grouped = _mk_grouped_pr(
            _body_with(["a", "b", "c"]),
            number=42,
            labels=[Label(name="caretaker:owned")],
        )
        # Title is the generic "group" form so _parse_bump() returns None
        # → the grouped PR is picked up by the bisector path.
        grouped.title = "chore(deps): bump the npm_and_yarn group with 3 updates"
        gh = _make_agent_github([grouped], ci_status="failure")

        agent = DependencyAgent(
            github=gh,
            owner="o",
            repo="r",
            post_digest=False,
            bisector_enabled=True,
        )
        report = await agent.run()

        assert 42 in report.bisector_plans_posted
        gh.add_issue_comment.assert_awaited_once()
        comment_body = gh.add_issue_comment.call_args.args[3]
        assert BISECTOR_COMMENT_MARKER in comment_body

    @pytest.mark.asyncio
    async def test_noop_without_owned_label(self) -> None:
        grouped = _mk_grouped_pr(_body_with(["a", "b"]), number=42, labels=[])
        grouped.title = "chore(deps): bump the npm_and_yarn group with 2 updates"
        gh = _make_agent_github([grouped], ci_status="failure")

        agent = DependencyAgent(
            github=gh,
            owner="o",
            repo="r",
            post_digest=False,
            bisector_enabled=True,
        )
        report = await agent.run()

        assert report.bisector_plans_posted == []
        gh.add_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_feature_disabled(self) -> None:
        grouped = _mk_grouped_pr(
            _body_with(["a", "b"]),
            number=42,
            labels=[Label(name="caretaker:owned")],
        )
        grouped.title = "chore(deps): bump the npm_and_yarn group with 2 updates"
        gh = _make_agent_github([grouped], ci_status="failure")

        agent = DependencyAgent(
            github=gh,
            owner="o",
            repo="r",
            post_digest=False,
            bisector_enabled=False,
        )
        report = await agent.run()

        assert report.bisector_plans_posted == []
        gh.add_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_when_ci_green(self) -> None:
        grouped = _mk_grouped_pr(
            _body_with(["a", "b"]),
            number=42,
            labels=[Label(name="caretaker:owned")],
        )
        grouped.title = "chore(deps): bump the npm_and_yarn group with 2 updates"
        gh = _make_agent_github([grouped], ci_status="success")

        agent = DependencyAgent(
            github=gh,
            owner="o",
            repo="r",
            post_digest=False,
            bisector_enabled=True,
        )
        await agent.run()

        gh.add_issue_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotent_on_existing_comment(self) -> None:
        import datetime as dt

        from caretaker.github_client.models import Comment, User

        existing = [
            Comment(
                id=7,
                user=User(login="caretaker[bot]", id=1),
                body=f"prior plan\n\n{BISECTOR_COMMENT_MARKER}",
                created_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
            )
        ]
        grouped = _mk_grouped_pr(
            _body_with(["a", "b"]),
            number=42,
            labels=[Label(name="caretaker:owned")],
        )
        grouped.title = "chore(deps): bump the npm_and_yarn group with 2 updates"
        gh = _make_agent_github([grouped], ci_status="failure", existing_comments=existing)

        agent = DependencyAgent(
            github=gh,
            owner="o",
            repo="r",
            post_digest=False,
            bisector_enabled=True,
        )
        report = await agent.run()

        assert report.bisector_plans_posted == []
        gh.add_issue_comment.assert_not_awaited()
