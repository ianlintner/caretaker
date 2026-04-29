"""Bootstrap Agent — opens a setup PR for newly-installed repos.

Triggered by the ``installation`` and ``installation_repositories``
webhook events. For each newly-attached repository, the agent:

1. Resolves the default branch and its head SHA.
2. Creates a ``caretaker/bootstrap`` branch (or no-ops if it already
   exists with our marker — the agent is fully idempotent).
3. Commits the consumer-side files: default maintainer config, agent
   persona docs, copilot-instructions append, pinned version. There is
   no consumer-side GitHub Actions workflow — all execution happens
   server-side, driven by App webhooks.
4. Opens a "chore: setup caretaker" PR listing what was added.

Skipped when the App's installation token already shows our marker
file present on the default branch — that means a previous bootstrap
PR was already merged or is open.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from caretaker.github_client.api import GitHubClient

logger = logging.getLogger(__name__)


# ── Files we drop in the bootstrap PR ─────────────────────────────────


_BRANCH_NAME = "caretaker/bootstrap"
_PR_TITLE = "chore: setup caretaker"
_MARKER_PATH = ".github/maintainer/.version"
_BOOTSTRAP_PR_LABEL = "caretaker:bootstrap"


class TemplateNotFoundError(RuntimeError):
    """Raised when a bootstrap template file cannot be located.

    Distinct from a generic ``RuntimeError`` so callers can distinguish
    "template missing" (a deployment-shape problem — e.g. installed
    non-editable in a Docker image where ``setup-templates/`` is not
    bundled) from a transient I/O failure.
    """


def _candidate_template_roots() -> list[Path]:
    """Return possible locations for the in-tree ``setup-templates/templates`` dir.

    1. ``parents[3]`` — editable / source-checkout install
       (``src/caretaker/bootstrap_agent/agent.py`` → repo root).
    2. ``parents[2]`` — when the package is installed at
       ``site-packages/caretaker/bootstrap_agent/agent.py``, ``parents[2]``
       is ``site-packages/`` itself; in that layout we expect the
       templates to be installed alongside via ``package_data``. The
       ``pyproject.toml`` MANIFEST should ship them as
       ``caretaker/_setup_templates/`` (not yet wired — fail loud below
       so the gap is visible rather than silent-blank-file in production).
    3. ``CARETAKER_BOOTSTRAP_TEMPLATES_DIR`` env override for operators
       who want to point the agent at a vendored template dir.
    """
    here = Path(__file__).resolve()
    roots: list[Path] = []
    env_override = os.environ.get("CARETAKER_BOOTSTRAP_TEMPLATES_DIR", "").strip()
    if env_override:
        roots.append(Path(env_override))
    # Source checkout: caretaker/.../agent.py → parents[3] is repo root
    roots.append(here.parents[3] / "setup-templates" / "templates")
    # Installed alongside the package (future-proofing for package_data shipping)
    roots.append(here.parents[1] / "_setup_templates")
    return roots


_TEMPLATE_PATHS = {
    ".github/maintainer/config.yml": ("config-default.yml",),
    ".github/agents/maintainer-pr.md": ("agents", "maintainer-pr.md"),
    ".github/agents/maintainer-issue.md": ("agents", "maintainer-issue.md"),
    ".github/agents/maintainer-upgrade.md": ("agents", "maintainer-upgrade.md"),
}


def _file_template(path: str) -> str:
    """Return the canonical contents the bootstrap PR commits at ``path``.

    Raises :class:`TemplateNotFoundError` when the template cannot be
    located in any candidate directory. We deliberately do *not* fall
    back to an empty string: a bootstrap PR with blank workflow YAML is
    worse than no PR at all — operators can't tell the bootstrap
    failed if the PR appears to succeed with empty files.
    """
    rel_parts = _TEMPLATE_PATHS.get(path)
    if rel_parts is None:
        raise TemplateNotFoundError(f"unknown bootstrap template: {path!r}")
    tried: list[str] = []
    for root in _candidate_template_roots():
        candidate = root.joinpath(*rel_parts)
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate.read_text()
    raise TemplateNotFoundError(
        f"bootstrap template not found for {path!r}; looked at: {tried}. "
        "Set CARETAKER_BOOTSTRAP_TEMPLATES_DIR to an explicit path if running "
        "from a non-source install."
    )


def _copilot_instructions_append() -> str:
    return (
        "\n## Caretaker\n\n"
        "This repo uses the [caretaker](https://github.com/ianlintner/caretaker) "
        "autonomous maintenance system. A centralised backend listens to this repo's "
        "GitHub App webhooks and posts structured comments on PRs and issues. All "
        "execution is backend-side; this repo holds only configuration and agent "
        "persona docs.\n\n"
        "Agent instruction files live in `.github/agents/`:\n"
        "- `maintainer-pr.md` — how to respond to PR fix requests\n"
        "- `maintainer-issue.md` — how to execute assigned issues\n"
        "- `maintainer-upgrade.md` — how to apply caretaker upgrades\n\n"
        "Always check these files when you receive a caretaker assignment.\n"
    )


def _pr_body() -> str:
    return (
        "Caretaker has been installed on this repository.\n\n"
        "This PR drops in the consumer-side configuration. After it merges, the "
        "centralised caretaker backend will receive this repo's GitHub App webhooks "
        "and run agents server-side — no PATs, no LLM keys, no consumer-side workflow, "
        "and no checkout-and-pip in your CI.\n\n"
        "## Files\n\n"
        "- `.github/maintainer/config.yml` — per-repo agent configuration.\n"
        "- `.github/maintainer/.version` — pinned caretaker version.\n"
        "- `.github/agents/maintainer-{pr,issue,upgrade}.md` — agent persona docs that "
        "tell `@copilot` how to respond to caretaker assignments.\n"
        "- `.github/copilot-instructions.md` — appended block describing the system.\n\n"
        "## Reviewer checklist\n\n"
        "- [ ] caretaker GitHub App is installed on this repo (it is — that's how this "
        "PR appeared).\n"
        "- [ ] Files look right; merge to enable caretaker.\n\n"
        "<!-- caretaker:bootstrap-pr -->"
    )


# ── Result envelope ───────────────────────────────────────────────────


@dataclass
class BootstrapReport:
    repos_attempted: list[str] = field(default_factory=list)
    prs_opened: list[tuple[str, int]] = field(default_factory=list)
    prs_skipped_existing: list[str] = field(default_factory=list)
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
        version: str | None = None,
        dry_run: bool = False,
        # Accepted for backwards compatibility; ignored. Repo-level
        # variables stopped being needed once the consumer-side workflow
        # was retired.
        backend_url: str | None = None,
        oidc_audience: str | None = None,
    ) -> None:
        del backend_url, oidc_audience
        self._github = github
        self._owner = owner
        self._repo = repo
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
            return outcome

        if self._dry_run:
            logger.info("bootstrap dry-run: would scaffold %s/%s", owner, repo)
            return outcome

        # Resolve all templates BEFORE we touch the consumer repo. A
        # missing template should fail loud and leave no half-bootstrapped
        # state behind (no orphan branch, no PR with blank workflow YAML).
        try:
            files_to_commit = self._files_to_commit()
        except TemplateNotFoundError:
            logger.exception("bootstrap aborted for %s/%s: template lookup failed", owner, repo)
            raise

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
        for path, content in files_to_commit:
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

        return outcome

    async def _default_branch(self, owner: str, repo: str) -> str:
        info = await self._github.get_repo(owner, repo)
        # Repository is a dataclass exposing default_branch directly; fall
        # back to ``main`` if the field is somehow missing.
        return getattr(info, "default_branch", "main") or "main"

    def _files_to_commit(self) -> list[tuple[str, str]]:
        """Resolve every template the bootstrap PR will commit.

        Raises :class:`TemplateNotFoundError` if any template is missing
        — we'd rather fail the bootstrap loudly than open a PR with
        blank workflow files. A failure here surfaces in the run log /
        admin SPA via the agent's error envelope.
        """
        templated_paths = list(_TEMPLATE_PATHS.keys())
        files: list[tuple[str, str]] = [
            (".github/maintainer/.version", f"{self._version}\n"),
        ]
        for path in templated_paths:
            files.append((path, _file_template(path)))
        return files

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
                body=_pr_body(),
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


@dataclass
class _BootstrapOutcome:
    pr_number: int | None = None
    skipped_existing: bool = False


__all__ = ["BootstrapAgent", "BootstrapReport"]
