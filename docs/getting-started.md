# Getting started

There are two common ways to use Caretaker:

1. **Consumer repository setup** — use the setup guide to install caretaker into another repository.
2. **Local development** — run caretaker directly from this repository while developing or debugging it.

## Consumer repository setup

The fastest path is the setup guide shipped with this repository:

- [`setup-templates/SETUP_AGENT.md`](https://github.com/ianlintner/caretaker/blob/main/setup-templates/SETUP_AGENT.md)

The guide is designed to be used from a GitHub issue assigned to `@copilot`.

Use the template below — click the **copy** button on the right to copy it, then paste it as the body of a new issue in your repository:

```markdown
## Setup Caretaker

@copilot Please set up the caretaker system for this repository.

### Instructions

1. Read the setup guide at:
   https://github.com/ianlintner/caretaker/blob/main/setup-templates/SETUP_AGENT.md
2. Follow the instructions exactly.
3. Open a single PR with the generated files.
```

## Local development

Install the project in editable mode:

```bash
pip install -e ".[dev,docs]"
```

Validate a config file:

```bash
caretaker validate-config --config .github/maintainer/config.yml
```

Run the orchestrator locally:

```bash
caretaker run --config .github/maintainer/config.yml
```

Run in dry-run mode:

```bash
caretaker run --config .github/maintainer/config.yml --dry-run
```

## Required environment

Caretaker expects a GitHub token when it runs against a repository:

- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY_OWNER`
- `GITHUB_REPOSITORY_NAME`

Alternatively, `GITHUB_REPOSITORY` can be provided in `owner/repo` format.

Optional, but strongly recommended for hands-free Copilot iteration:

- `COPILOT_PAT` from a write-capable user or machine user; caretaker uses it for API-based Copilot issue assignment and for workflow-authored `@copilot` PR comments so they are attributed to that identity instead of `github-actions[bot]`
- `ANTHROPIC_API_KEY` for enhanced Claude-backed reasoning features

## Key installed files

A configured repository typically gets:

- `.github/maintainer/config.yml` — repo-specific config
- `.github/maintainer/.version` — pinned caretaker version
- `.github/copilot-instructions.md` — global Copilot project memory
- `.github/agents/*.md` — per-agent personas

No workflow file is installed. Caretaker runs server-side on AKS and
receives GitHub App webhooks; consumer repos only ship config and
Copilot instructions. See [Architecture](architecture.md) for the
runtime topology.
