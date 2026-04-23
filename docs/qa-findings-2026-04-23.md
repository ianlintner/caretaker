# QA Run Findings — 2026-04-23 (caretaker-qa testbed)

This document captures the concrete gaps, bugs, and onboarding friction
discovered during a full end-to-end QA run of caretaker against the
[ianlintner/caretaker-qa](https://github.com/ianlintner/caretaker-qa)
testbed. Each finding is actionable and links to either code references
or shipped fixes. Recommendations are ordered roughly by user impact.

## 1. `claude_enabled: "false"` silently kills the whole LLM router (**CRITICAL**)

### What happened
`pr_reviewer` never produced inline reviews. Every PR ended up routed to
the claude-code hand-off path, even for trivial diffs that should have
gone inline. Root cause was a single config line:

```yaml
llm:
  claude_enabled: "false"   # comment claimed this just suppressed the ANTHROPIC_API_KEY probe
```

### Why it's dangerous
`src/caretaker/llm/router.py:42-66` implements a tri-state:

```python
if config.claude_enabled == "auto":
    self._active = self._claude.available
elif config.claude_enabled == "true":
    self._active = self._claude.available
    ...
else:                          # ← "false" lands here
    self._active = False       # ← entire LLM router goes inactive
```

`_active=False` means `claude_available` returns False, `feature_enabled()`
always returns False, and every agent that asks "is the LLM available?"
falls back to its non-LLM path. In `pr_reviewer` this degrades to "route
everything to claude-code" — which looks like the reviewer is working,
but it never actually uses the configured LLM provider (Azure AI,
OpenAI, etc.) for inline reviews.

Critically, `claude_enabled` in the name suggests it only toggles the
*Claude/Anthropic* provider. It does not. It toggles the **router itself**.
LiteLLM provider selection (`provider: litellm`, Azure AI Foundry, etc.)
is completely cut off by `claude_enabled: "false"`.

### Recommended fixes
1. **Add a pydantic `Field(description=...)` docstring** on
   `LLMConfig.claude_enabled` at `src/caretaker/config.py:219` making
   the side-effect explicit:
   > "Controls whether ANY LLM provider is active. 'false' disables
   > the router entirely including LiteLLM / Azure AI / OpenAI.
   > Use 'auto' (default) to let credentials auto-detect the provider."
2. **Rename the field** (with migration alias) to something decoupled
   from Claude — e.g. `llm_enabled: auto|true|false` + a separate
   `anthropic_probe: auto|force|suppress` if the Anthropic-probe case
   is still needed.
3. **Emit a WARNING log when `_active` is False and `provider=litellm`
   with valid credentials present** — this is almost always a misconfig.
4. **Audit existing config.yml samples and docs** for `claude_enabled:
   "false"` and change them to `auto`.

---

## 2. `releases.json` is not updated automatically on release (process gap)

### What happened
The upgrade agent reported the testbed was already on the latest
version (0.16.0). In reality main had already shipped v0.17.0 via PR
#520 plus several intermediate releases. Reason: `releases.json` in
the main repo was truncated at v0.11.0 — entries for v0.12.0 through
v0.17.0 had never been added.

### Why it's dangerous
The upgrade agent's only source of truth for "what upgrades exist" is
`releases.json`. If that file isn't updated at release time, every
downstream deployment silently loses the upgrade-tracking signal. This
mode failed silently for several releases.

### Recommended fixes
1. **Add a release-automation GitHub Action** that, on `release:
   published`, appends an entry to `releases.json` with the tag name,
   date, and release body (mapped to `upgrade_notes`) and opens a PR
   to the main branch.
2. **Alternatively**, treat `releases.json` as a required artifact
   in the release checklist and add a CI check that fails the
   `release` workflow if the newest tag isn't present.
3. Fixed in this session by backfilling 7 entries manually
   (commit `89c0d81` in main caretaker repo).

---

## 3. Minimum workflow permissions are not documented or templated

### What happened
The testbed `maintainer.yml` workflow was missing `checks: write` and
`security-events: read`, producing 403s on every run:

- POST `/repos/.../check-runs` — can't post caretaker pr-readiness check
- GET `/repos/.../code-scanning/alerts` — can't read CodeQL alerts
- GET `/repos/.../secret-scanning/alerts` — always 403 for public repos
  regardless of token scope (see finding #4)

### Recommended fixes
1. **Document the minimum `permissions:` block** in README /
   `docs/getting-started.md`:
   ```yaml
   permissions:
     contents: write
     issues: write
     pull-requests: write
     checks: write            # for pr-readiness check-runs
     security-events: read    # for code-scanning alerts (optional)
   ```
2. **Add a `caretaker init-workflow` CLI** (or template in `examples/`)
   that emits a correct `maintainer.yml` with these permissions
   pre-filled.
3. **scope_gap reporter already maps these 403s** to the missing
   permissions (`src/caretaker/github_client/scope_gap.py:49-51`) —
   promote that output to a dedicated onboarding-digest section so
   it's impossible to miss.

---

## 4. `security_agent` probes unavailable features by default (DX)

### What happened
`config.yml` defaults for `security_agent`:
```yaml
security_agent:
  include_dependabot: true
  include_code_scanning: true
  include_secret_scanning: true
```

But on **public repos**, `/secret-scanning/alerts` returns 403 regardless
of GITHUB_TOKEN scope (secret-scanning alert read is gated on Advanced
Security licensing for public repos). This caused noise on every run.

Similarly, `code_scanning` returns 403 unless CodeQL is actually
configured.

### Recommended fixes
1. **Detect availability at first-run** — probe each endpoint with a
   1-second HEAD/GET and cache a `features_available` bitmask.
   Skip future probes for features that returned 403/404.
2. **Emit a config suggestion** in the scope-gap digest when we detect
   persistent 403s:
   > "Your token can't read `/secret-scanning/alerts` on this public
   > repo. Set `security_agent.include_secret_scanning: false` to
   > silence this probe."
3. **Change defaults** to `include_secret_scanning: false` for public
   repos without GHAS, or add a `plan: free|pro|enterprise` auto-detect
   that flips these safely.

---

## 5. `pr_reviewer` defaults are too conservative for non-webhook deployments

### What happened
`PRReviewerConfig` at `src/caretaker/config.py:479-514`:

```python
webhook_only: bool = True
trigger_actions: list[str] = ["opened"]
```

Many caretaker deployments (including the testbed) run on a **scheduled
cron without a webhook bridge**. In this mode:
- `webhook_only=True` means `pr_reviewer` sees events only from the
  webhook dispatcher — which doesn't exist in polling mode → agent
  never runs.
- `trigger_actions=["opened"]` means even with webhook, re-reviews
  after a Copilot force-push are skipped.

### Recommended fixes
1. **Default `webhook_only: False`** — this is the safe setting for
   both polling and webhook deployments.
2. **Default `trigger_actions: ["opened", "synchronize", "reopened",
   "ready_for_review"]`** — covers draft-to-ready and push updates.
3. **Add a `deployment_mode: webhook|polling|hybrid`** preset that
   sets this + 5 other related flags at once so users don't have to
   tune individual flags.

---

## 6. Workflow template missing `pull_request.ready_for_review` trigger

### What happened
Copilot-bot PRs start as **drafts**. The default `maintainer.yml`
template used only `pull_request.types: [opened, synchronize]` —
which never fires when a draft is marked ready-for-review. Result:
`pr_reviewer` silently skipped every Copilot PR until we added
`ready_for_review` to the trigger list.

### Recommended fix
Add `ready_for_review` to the default `pull_request.types` in any
caretaker workflow template / quickstart.

---

## 7. GitHub Actions owner-approval gate on bot-triggered PR runs (operational)

### What happened
When Copilot-bot (or any bot actor) pushes to a PR branch or an
`issue_comment` mentions @copilot, the resulting workflow runs
(CI, CodeQL, caretaker itself) land with `conclusion=action_required`,
blocking merges. GitHub requires manual owner approval for these runs
and the `POST /actions/runs/{id}/approve` REST endpoint refuses with
"This run is not from a fork pull request" because it's hardcoded to
the fork-PR path.

### Why it's a problem for automated review/fix loops
caretaker's end-to-end flow for Copilot-delegated fixes is:
1. caretaker opens an issue/PR targeting Copilot
2. Copilot pushes commits
3. caretaker re-reviews
4. caretaker (or human) merges

Step 3 is silently blocked because CI never runs. There's no programmatic
way to approve from outside the UI. caretaker has no visibility that
runs are stuck in `action_required`.

### Recommended fixes
1. **Document this as a known limitation** in the onboarding docs —
   owners must either (a) approve via the Actions tab UI, or (b)
   disable the "Require approval for first-time contributors" setting.
2. **New optional agent: `pr_ci_approver`** that:
   - watches for `action_required` runs on whitelisted actors
     (`github-actions[bot]`, `the-care-taker[bot]`, `Copilot`, etc.)
   - surfaces them in the orchestrator digest
   - optionally escalates with a maintainer:escalated label so humans
     know to approve
3. **scope_gap reporter enhancement** — detect a run in `action_required`
   and add a helpful digest entry explaining the manual step.

---

## 8. Review-agent value demonstrated — suggest promoting in docs

### What happened
Once `claude_enabled` was flipped to `auto`, `pr_reviewer` posted
inline reviews on 9 open PRs and found **real bugs**:

| PR | Real bug found |
|----|----------------|
| #14 (dispatch-guard) | `issues:labeled` skip was too broad — needs actor-filter to `the-care-taker[bot]` / `github-actions[bot]` |
| #15 (deceptive markdown) | Naive regex doesn't handle parens in URLs; lowercasing full URL breaks case-sensitive paths |
| #16 (sanitize_input) | `<[^>]+>` strips legitimate text like `1 < 2 > 0`; sanitize-before-truncate ordering was wrong |
| #17 (HTTP 429 retry) | `return True` fallthrough made ALL non-`HTTPStatusError` exceptions retriable, broadening retry behavior beyond the docstring |

### Recommended fixes
1. **Add these 4 bugs as regression fixtures** in caretaker-qa (they
   already exist as scenarios 09, 07, 06, 05 — but add the exact
   review-verdict text as golden-file assertions).
2. **Promote `pr_reviewer` in the README** as a "code review agent
   that finds real bugs in AI-generated PRs" — currently it's
   under-advertised.
3. **Add a showcase section to docs** with before/after from this
   QA session (sanitized PR links).

---

## Appendix — fixes shipped during this QA session

### Main caretaker repo
| Commit | Description |
|--------|-------------|
| `89c0d81` | Backfill releases.json for v0.12.0 → v0.17.0 |

### caretaker-qa repo (testbed)
| Commit | Description |
|--------|-------------|
| `f13e58c` | Bump caretaker to v0.17.0 |
| `80bbc3a` | Widen workflow GITHUB_TOKEN scopes (`checks:write`, `security-events:read`) |
| `2962b9b` | Plumb AZURE_AI_API_KEY / AZURE_AI_API_BASE into all caretaker steps |
| `a297be4` | Disable security-agent probes for features not wired up |
| `639b5323` | Enable pr_reviewer polling + broaden `trigger_actions` |
| `c21febc` | Add `pull_request.ready_for_review` trigger |
| `4c0cb94a` | Set `claude_enabled: auto` so LLM router activates with Azure AI |

---

*Generated from caretaker-qa run 24834090790 (2026-04-23T12:05 UTC).*
