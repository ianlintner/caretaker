"""Bootstrap Agent — opens a setup PR + sets repo variables for newly-installed repos.

Triggered by the ``installation`` and ``installation_repositories``
webhook events. For each newly-attached repository, the agent:

1. Resolves the default branch and its head SHA.
2. Creates a ``caretaker/bootstrap`` branch (or no-ops if it already
   exists with our marker — the agent is fully idempotent).
3. Commits the consumer-side files: thin streaming workflow, default
   maintainer config, agent persona docs, copilot-instructions append,
   pinned version.
4. Opens a "chore: setup caretaker" PR listing what was added and what
   the owner needs to do (review + merge).
5. Sets the ``CARETAKER_BACKEND_URL`` repository variable so the thin
   workflow has the backend URL it needs without operator action.

Skipped when the App's installation token already shows our marker
file present on the default branch — that means a previous bootstrap
PR was already merged or is open.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)


# ── Files we drop in the bootstrap PR ─────────────────────────────────


_BRANCH_NAME = "caretaker/bootstrap"
_PR_TITLE = "chore: setup caretaker"
_MARKER_PATH = ".github/maintainer/.version"
_BOOTSTRAP_PR_LABEL = "caretaker:bootstrap"
_BACKEND_URL_VAR = "CARETAKER_BACKEND_URL"
_OIDC_AUDIENCE_VAR = "CARETAKER_OIDC_AUDIENCE"


def _file_template(path: str) -> str:
    """Return the canonical contents the bootstrap PR commits at ``path``.

    The file content is loaded lazily from the in-tree
    ``setup-templates/templates/`` tree so a single source of truth
    serves both the human-readable docs and the auto-bootstrap flow.
    """
    from pathlib import Path

    # ``setup-templates/`` is a sibling of ``src/`` at the repo root.
    repo_root = Path(__file__).resolve().parents[3]
    template_root = repo_root / "setup-templates" / "templates"
    candidates = {
        ".github/workflows/maintainer.yml": template_root / "workflows" / "maintainer.yml",
        ".github/maintainer/config.yml": template_root / "config-default.yml",
        ".github/agents/maintainer-pr.md": template_root / "agents" / "maintainer-pr.md",
        ".github/agents/maintainer-issue.md": template_root / "agents" / "maintainer-issue.md",
        ".github/agents/maintainer-upgrade.md": template_root / "agents" / "maintainer-upgrade.md",
    }
    src = candidates.get(path)
    if src is None or not src.is_file():
        return ""
    return src.read_text()


def _copilot_instructions_append() -> str:
    return (
        "\n## Caretaker\n\n"
        "This repo uses the [caretaker](https://github.com/ianlintner/caretaker) "
        "autonomous maintenance system. A centralised backend listens to this repo's "
        "GitHub App webhooks and posts structured comments on PRs and issues; the "
        "workflow at `.github/workflows/maintainer.yml` is a thin fallback that streams "
        "runs from the backend.\n\n"
        "Agent instruction files live in `.github/agents/`:\n"
        "- `maintainer-pr.md` — how to respond to PR fix requests\n"
        "- `maintainer-issue.md` — how to execute assigned issues\n"
        "- `maintainer-upgrade.md` — how to apply caretaker upgrades\n\n"
        "Always check these files when you receive a caretaker assignment.\n"
    )


def _pr_body(*, backend_url: str | None) -> str:
    backend_line = (
        f"- Repository variable `{_BACKEND_URL_VAR}` set to `{backend_url}` (set by caretaker)."
        if backend_url
        else f"- ⚠️ Repository variable `{_BACKEND_URL_VAR}` is **not set** — set it in "
        "*Settings → Secrets and variables → Actions → Variables* before merging."
    )
    return (
        "Caretaker has been installed on this repository.\n\n"
        "This PR drops in the consumer-side configuration. After it merges, the "
        "centralised caretaker backend will receive this repo's GitHub App webhooks "
        "and run agents server-side — no PATs, no LLM keys, and no checkout-and-pip "
        "in your CI.\n\n"
        "## Files\n\n"
        "- `.github/workflows/maintainer.yml` — thin streaming workflow (runs only on "
        "operator-triggered `workflow_dispatch` and a sparse 6h cron as a webhook-miss "
        "fallback). Webhooks are the primary path.\n"
        "- `.github/maintainer/config.yml` — per-repo agent configuration.\n"
        "- `.github/maintainer/.version` — pinned caretaker version.\n"
        "- `.github/agents/maintainer-{pr,issue,upgrade}.md` — agent persona docs that "
        "tell `@copilot` how to respond to caretaker assignments.\n"
        "- `.github/copilot-instructions.md` — appended block describing the system.\n\n"
        "## Variables\n\n"
        f"{backend_line}\n\n"
        "## Reviewer checklist\n\n"
        "- [ ] caretaker GitHub App is installed on this repo (it is — that's how this "
        "PR appeared).\n"
        f"- [ ] `{_BACKEND_URL_VAR}` repo variable is set.\n"
        "- [ ] Files look right; merge to enable caretaker.\n\n"
        "<!-- caretaker:bootstrap-pr -->"
    )


# ── Result envelope ───────────────────────────────────────────────────


@dataclass
class BootstrapReport:
    repos_attempted: list[str] = field(default_factory=list)
    prs_opened: list[tuple[str, int]] = field(default_factory=list)
    prs_skipped_existing: list[str] = field(default_factory=list)
    variables_set: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── Agent ─────────────────────────────────────────────────────────────


class BootstrapAgent:
    """Auto-scaffolds caretaker into a newly-installed repo.

    The agent is event-driven: invoked from
    ``installation.created`` / ``installation_repositories.added``
    webhook payloads. It is also safe to call directly (e.g. from an
    admin endpoint) by passing a synthetic event payload with
    ``repositories_added``.
    """

    def __init__(
        self,
        github: GitHubClient,
        owner: str,
        repo: str,
        *,
        backend_url: str | None = None,
        oidc_audience: str | None = None,
        version: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._github = github
        self._owner = owner
        self._repo = repo
        self._backend_url = (
            backend_url or os.environ.get("CARETAKER_PUBLIC_BACKEND_URL", "").strip() or None
        )
        self._oidc_audience = (
            oidc_audience or os.environ.get("CARETAKER_OIDC_GITHUB_AUDIENCE", "").strip() or None
        )
        self._version = version or os.environ.get("CARETAKER_VERSION", "").strip() or "0.24.0"
        self._dry_run = dry_run

    async def run(self, event_payload: dict[str, Any] | None = None) -> BootstrapReport:
        """Bootstrap every repo named in the event payload.

        ``installation.created`` carries ``repositories``; the
        ``installation_repositories.added`` event carries
        ``repositories_added``. Both shapes are handled here.
        """
        report = BootstrapReport()
        repos = self._target_repos(event_payload)
        if not repos:
            # Default: bootstrap the single repo this AgentContext was
            # built for. Useful when the agent is invoked outside the
            # installation event flow (admin re-bootstrap, tests).
            repos = [(self._owner, self._repo)]

        for owner, repo in repos:
            full = f"{owner}/{repo}"
            report.repos_attempted.append(full)
            try:
                outcome = await self._bootstrap_one(owner, repo)
                if outcome.pr_number is not None:
                    report.prs_opened.append((full, outcome.pr_number))
                if outcome.skipped_existing:
                    report.prs_skipped_existing.append(full)
                report.variables_set.extend(f"{full}/{name}" for name in outcome.variables_set)
            except Exception as exc:
                logger.exception("bootstrap failed for %s: %s", full, exc)
                report.errors.append(f"{full}: {exc!r}")

        return report

    @staticmethod
    def _target_repos(payload: dict[str, Any] | None) -> list[tuple[str, str]]:
        if not payload:
            return []
        candidates: list[dict[str, Any]] = []
        # installation_repositories.added payload
        added = payload.get("repositories_added")
        if isinstance(added, list):
            candidates.extend(item for item in added if isinstance(item, dict))
        # installation.created payload
        repos = payload.get("repositories")
        if isinstance(repos, list):
            candidates.extend(item for item in repos if isinstance(item, dict))

        out: list[tuple[str, str]] = []
        for entry in candidates:
            full = entry.get("full_name") or ""
            if "/" in full:
                owner, _, repo = full.partition("/")
                out.append((owner, repo))
        return out

    async def _bootstrap_one(self, owner: str, repo: str) -> _BootstrapOutcome:
        """Bootstrap a single repo. Idempotent."""
        outcome = _BootstrapOutcome()

        # Idempotency check — if our marker file already exists on the
        # default branch, the bootstrap PR has already merged and we
        # should not open a fresh one.
        existing = await self._github.get_file_contents(owner, repo, _MARKER_PATH)
        if existing is not None:
            logger.info("bootstrap skipped: %s/%s already has %s", owner, repo, _MARKER_PATH)
            outcome.skipped_existing = True
            # Variables can still be (re-)set safely; _set_variables is idempotent.
            outcome.variables_set = await self._set_variables(owner, repo)
            return outcome

        if self._dry_run:
            logger.info("bootstrap dry-run: would scaffold %s/%s", owner, repo)
            return outcome

        # Resolve default branch + head SHA.
        default_branch = await self._default_branch(owner, repo)
        head_sha = await self._github.get_default_branch_sha(owner, repo, default_branch)

        # Create / reuse the bootstrap branch.
        try:
            await self._github.create_branch(owner, repo, _BRANCH_NAME, head_sha)
        except Exception as exc:
            # 422 = branch already exists. Not an error — fall through and
            # write commits onto it (create_or_update_file handles the rest).
            if "422" not in str(exc):
                logger.warning("bootstrap branch create failed %s/%s: %s", owner, repo, exc)

        # Commit the bootstrap files. Each call commits a single file.
        files = self._files_to_commit()
        for path, content in files:
            await self._github.create_or_update_file(
                owner=owner,
                repo=repo,
                path=path,
                message=f"chore(caretaker): scaffold {path}",
                content=content,
                branch=_BRANCH_NAME,
            )

        # Append the copilot-instructions block. Special-cased because it
        # MERGES with any existing file rather than overwriting.
        await self._upsert_copilot_instructions(owner, repo)

        # Open the PR (or skip if one already exists from a prior partial run).
        pr_number = await self._open_pr_if_absent(owner, repo, default_branch)
        outcome.pr_number = pr_number

        # Set repo variables. Best-effort — bootstrap PR is still useful
        # if variable-set fails (operator can set manually).
        outcome.variables_set = await self._set_variables(owner, repo)

        return outcome

    async def _default_branch(self, owner: str, repo: str) -> str:
        info = await self._github.get_repo(owner, repo)
        # Repository is a dataclass exposing default_branch directly; fall
        # back to ``main`` if the field is somehow missing.
        return getattr(info, "default_branch", "main") or "main"

    def _files_to_commit(self) -> list[tuple[str, str]]:
        wf = ".github/workflows/maintainer.yml"
        cfg = ".github/maintainer/config.yml"
        agent_pr = ".github/agents/maintainer-pr.md"
        agent_issue = ".github/agents/maintainer-issue.md"
        agent_up = ".github/agents/maintainer-upgrade.md"
        return [
            (".github/maintainer/.version", f"{self._version}\n"),
            (wf, _file_template(wf)),
            (cfg, _file_template(cfg)),
            (agent_pr, _file_template(agent_pr)),
            (agent_issue, _file_template(agent_issue)),
            (agent_up, _file_template(agent_up)),
        ]

    async def _upsert_copilot_instructions(self, owner: str, repo: str) -> None:
        path = ".github/copilot-instructions.md"
        existing = await self._github.get_file_contents(owner, repo, path)

        if existing is None:
            new_content = _copilot_instructions_append().lstrip()
            await self._github.create_or_update_file(
                owner=owner,
                repo=repo,
                path=path,
                message="chore(caretaker): add copilot-instructions",
                content=new_content,
                branch=_BRANCH_NAME,
            )
            return

        import base64

        try:
            decoded = base64.b64decode(existing.get("content", "").replace("\n", "")).decode(
                "utf-8"
            )
        except Exception:
            logger.warning(
                "could not decode existing copilot-instructions for %s/%s; overwriting",
                owner,
                repo,
            )
            decoded = ""

        if "## Caretaker" in decoded:
            # Already appended; do nothing.
            return

        new_content = decoded.rstrip() + "\n" + _copilot_instructions_append()
        await self._github.create_or_update_file(
            owner=owner,
            repo=repo,
            path=path,
            message="chore(caretaker): append copilot-instructions block",
            content=new_content,
            branch=_BRANCH_NAME,
            sha=existing.get("sha"),
        )

    async def _open_pr_if_absent(self, owner: str, repo: str, default_branch: str) -> int | None:
        # Best-effort: skip if a PR for this branch is already open.
        # We don't enumerate (no helper for "list PRs by head"); just try
        # to create and treat 422 as "already exists".
        try:
            data = await self._github.create_pull_request(
                owner=owner,
                repo=repo,
                title=_PR_TITLE,
                body=_pr_body(backend_url=self._backend_url),
                head=_BRANCH_NAME,
                base=default_branch,
                labels=[_BOOTSTRAP_PR_LABEL],
            )
            num = int(data.get("number", 0))
            return num if num > 0 else None
        except Exception as exc:
            if "422" in str(exc):
                logger.info("bootstrap PR already exists for %s/%s; skipping create", owner, repo)
                return None
            raise

    async def _set_variables(self, owner: str, repo: str) -> list[str]:
        set_names: list[str] = []
        if self._backend_url:
            try:
                await self._github.set_repo_variable(
                    owner, repo, _BACKEND_URL_VAR, self._backend_url
                )
                set_names.append(_BACKEND_URL_VAR)
            except Exception as exc:
                logger.warning(
                    "bootstrap could not set %s on %s/%s: %s",
                    _BACKEND_URL_VAR,
                    owner,
                    repo,
                    exc,
                )
        if self._oidc_audience:
            try:
                await self._github.set_repo_variable(
                    owner, repo, _OIDC_AUDIENCE_VAR, self._oidc_audience
                )
                set_names.append(_OIDC_AUDIENCE_VAR)
            except Exception as exc:
                logger.warning(
                    "bootstrap could not set %s on %s/%s: %s",
                    _OIDC_AUDIENCE_VAR,
                    owner,
                    repo,
                    exc,
                )
        return set_names


@dataclass
class _BootstrapOutcome:
    pr_number: int | None = None
    skipped_existing: bool = False
    variables_set: list[str] = field(default_factory=list)


__all__ = ["BootstrapAgent", "BootstrapReport"]
