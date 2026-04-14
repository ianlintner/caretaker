# Getting started

There are two common ways to use Caretaker:

1. **Consumer repository setup** — use the setup guide to install caretaker into another repository.
2. **Local development** — run caretaker directly from this repository while developing or debugging it.

## Consumer repository setup

The fastest path is the setup guide shipped with this repository:

- [`dist/SETUP_AGENT.md`](https://github.com/ianlintner/caretaker/blob/main/dist/SETUP_AGENT.md)

The guide is designed to be used from a GitHub issue assigned to `@copilot`.

A minimal issue looks like this:

```markdown
## Setup Caretaker

@copilot Please set up the caretaker system for this repository.

### Instructions

1. Read the setup guide at:
   https://github.com/ianlintner/caretaker/blob/main/dist/SETUP_AGENT.md
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

Optional:

- `ANTHROPIC_API_KEY` for enhanced Claude-backed reasoning features

## Key installed files

A configured repository typically gets:

- `.github/workflows/maintainer.yml`
- `.github/maintainer/config.yml`
- `.github/copilot-instructions.md`
- `.github/agents/*.md`

Those files define the workflow triggers, repo-specific config, and agent instructions that let the orchestrator and Copilot work together.
