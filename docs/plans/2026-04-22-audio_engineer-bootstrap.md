# audio_engineer pre-orchestrator bootstrap outage — 2026-04-22

## Summary

Every push-triggered `Caretaker` run on `ianlintner/audio_engineer` has
failed at GitHub's workflow-file parse step since 2026-04-20T08:26Z. No
job is scheduled, no `setup-python` or `pip install` fires, and
caretaker never emits an orchestrator RunSummary. GitHub surfaces this
as a generic "workflow file issue" and the run is over in 0s. The
2026-04-22 fleet audit mis-attributed the failures to the runtime-level
403 scope gaps already documented as P2 — this is strictly upstream, a
YAML-level parse failure.

## Failing runs

All eight push runs since 2026-04-22T05:53Z failed at the
workflow-file-parse step:

- `24763715193`, `24763331115`, `24762617294`,
- `24754140054`, `24754138858`, `24754137706`, `24754136434`, `24754134399`.

`gh run view <id> --log-failed` returns 404 on every one — the logs
rolled off, but there is nothing in them regardless: GitHub never
scheduled a job. `gh api /repos/ianlintner/audio_engineer/actions/runs/24763715193/jobs`
returns an empty `jobs` array on a completed/failed run, which is
GitHub's signature for a workflow that was rejected at YAML parse.

## Root cause

Commit [`e38c16f5`](https://github.com/ianlintner/audio_engineer/commit/e38c16f5)
landed on 2026-04-20T08:31Z as part of PR #62
("chore: upgrade caretaker to v0.10.0"). The second commit in that PR —
"fix: add workflows: write permission to maintainer workflow" — added a
fifth key to the job-level `permissions:` block:

```yaml
permissions:
  contents: write
  issues: write
  pull-requests: write
  workflows: write     # ← added 2026-04-20, rejected by GitHub
```

`workflows` is **not** a valid GITHUB_TOKEN permission scope in a
workflow file. The authoritative list is the GITHUB_TOKEN table in
GitHub's docs (`actions`, `checks`, `contents`, `deployments`,
`id-token`, `issues`, `discussions`, `packages`, `pages`, `pull-requests`,
`repository-projects`, `security-events`, `statuses`,
`attestations`, `models`). GitHub's workflow validator refuses to
schedule the run and the failing run's `name` falls back to the
file path `.github/workflows/maintainer.yml` instead of the `Caretaker`
name declared on line 1 — another tell that the YAML was never
successfully parsed past the permissions block.

The preceding commit (2026-04-17, `76ae4d1f`, "upgrade caretaker to
v0.5.2") did not have the key; the last successful run was on that
version, 2026-04-20T08:25Z (ID `24712826971`).

## Confirmation

A fresh `gh workflow run maintainer.yml -R ianlintner/audio_engineer`
dispatch attempt at 2026-04-22T11:03Z returned:

```
HTTP 422: failed to parse workflow:
(Line: 35, Col: 3): Unexpected value 'workflows'
```

Line 35 col 3 is exactly the `workflows: write` line. This is the
fresh log excerpt the stale runs no longer carry, and it is
deterministic — every invocation of the API against the committed HEAD
of `main` returns the identical error.

## Remediation (landed)

Two-part fix, shipped in one `caretaker` PR plus one `audio_engineer`
PR:

1. **`caretaker` PR** (`fix/audio-engineer-bootstrap`):
   - New `caretaker doctor --bootstrap-check` subcommand. Offline
     preflight with zero outbound network calls validating four things:
     `caretaker` imports, the config YAML parses on the pinned tag, the
     version-pin file is present and looks like semver, and every env
     var declared by an enabled config block is set. Exit 0/1/2 matches
     the existing `doctor` exit-code matrix. `--json` emits machine
     output on stdout; human table still goes to stderr.
   - Shipped workflow template wires
     `caretaker doctor --bootstrap-check` as a dedicated step after
     `Install caretaker` and before the main `Run`. Future consumers
     syncing the workflow pick it up for free.
   - Tests cover import check, config parse (missing, bad YAML, unknown
     key rejected by `StrictBaseModel`), version-pin validation, env-var
     enabled-vs-disabled distinction, and CLI surface. Full suite
     1653 passed, 1 skipped. Ruff + format + mypy clean.
   - Bootstrap-check cannot catch *this specific* outage — the workflow
     YAML never reached the job's steps, so the subcommand never
     executes — but it catches every sibling class: bad pin,
     unparseable config on a new pin, enabled block with no env var.
     Next such outage surfaces a clear actionable FAIL row instead of
     a bare "workflow file issue" placeholder.

2. **`audio_engineer` PR** (from this worktree):
   - Remove the `workflows: write` line from
     `.github/workflows/maintainer.yml`. Keeps the three valid scopes
     (`contents/issues/pull-requests: write`) that the caretaker-shipped
     template declares.
   - Once merged, workflow parses cleanly. The `schedule`-triggered run
     at 08:00Z is the success criterion: it must produce an orchestrator
     RunSummary, not a parse-level failure. Run URL posted in the
     `audio_engineer` PR body once available.

## Not fixed here

- **The test that introduced the bad key.** PR #62's commit message
  cites "Required by repo test `tests/test_maintainer_workflow.py`" for
  the `workflows: write` addition. That test needs to be deleted or
  corrected to assert the valid three-scope permissions set, not a
  non-existent `workflows` scope. Flagged in the `audio_engineer` PR
  body; a full fix is out-of-scope for this bootstrap unblock.
- **The five rotting Dependabot PRs** (#64 – #68). They accumulated
  because caretaker never ran to claim them; once the bootstrap is
  unblocked and the workflow successfully runs one more time, the
  triage subsystem (enabled via PR #467, currently in `dry_run`)
  will pick them up on the next scheduled invocation.
