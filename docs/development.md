# Development

## Local setup

Install the project with development and docs dependencies:

```bash
pip install -e ".[dev,docs]"
```

## Useful commands

### Run tests

```bash
pytest tests/ -v
```

### Lint

```bash
ruff check src/ tests/
ruff format --check src/ tests/
```

### Type-check

```bash
mypy src/
```

### Build docs

```bash
mkdocs build --strict
```

### Serve docs locally

```bash
mkdocs serve
```

## Important directories

| Path                 | Purpose                                                  |
| -------------------- | -------------------------------------------------------- |
| `src/caretaker/`     | source package                                           |
| `tests/`             | unit and integration-style tests                         |
| `schema/`            | versioned config schema                                  |
| `setup-templates/`   | consumer repo templates                                  |
| `infra/k8s/`         | AKS Deployment manifests reconciled by Flux              |
| `Dockerfile.mcp`     | image used by `mcp_backend` and `caretaker-job-dispatcher` |
| `.github/workflows/` | CI for this repo (lint, test, image build, docs publish) |
| `docs/`              | documentation site content                               |

## CI expectations

The main CI workflow validates:

- Ruff linting
- Ruff formatting
- mypy strict checks
- pytest with coverage

The docs workflow separately builds the MkDocs site and publishes it to GitHub Pages on `main`.

A separate image-build workflow publishes
`gabby.azurecr.io/caretaker-mcp:latest` on each merge to `main`; Flux
then reconciles the new image into AKS for `mcp_backend` and
`caretaker-job-dispatcher`. See [Architecture](architecture.md) for
the runtime topology.

## Documentation publishing

- `mkdocs.yml` defines the site structure
- `.github/workflows/docs.yml` builds and publishes the site
- `.readthedocs.yml` allows the same site to be built by Read the Docs-compatible tooling

## Contribution notes

When editing agent behavior:

- keep policy changes narrow and testable
- update docs when workflow expectations change
- prefer explicit config over hidden defaults
- run the full test suite before merging
