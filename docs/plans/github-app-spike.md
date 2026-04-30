# GitHub App Spike: Copilot SWE-Agent Assignment Verification

**Status:** Planned — Phase 1 prerequisite  
**Estimated effort:** 1 day  
**Owner:** TBD  
**Blocks:** Phase 2 full agent wiring

---

## Objective

Verify whether GitHub's Copilot SWE-agent assignment API (`POST
/repos/{owner}/{repo}/issues/{issue_number}/copilot`) accepts an **App
installation token** (server-to-server) or whether it strictly requires a
**user-to-server token** (i.e., a token issued on behalf of a specific GitHub
user account that has a Copilot seat).

This is the one remaining question from the GitHub App plan (§4: Copilot SWE-Agent Assignment — the gating question) that cannot be answered from documentation alone.

---

## Background

The current caretaker code uses `COPILOT_PAT` — a Personal Access Token
belonging to a human user — when it calls the Copilot assignment endpoint.
The GitHub App model replaces PATs with short-lived **installation tokens**
(`ghs_…`) issued by the App rather than by a user.

GitHub's documentation states that certain Copilot APIs require
`user-to-server` OAuth tokens, but it is not consistently clear whether the
SWE-agent assignment endpoint falls into that category in practice.

**Scenarios being tested:**

| Scenario | Token type                           | Hypothesis                                 |
| -------- | ------------------------------------ | ------------------------------------------ |
| S1       | App installation token (`ghs_…`)     | ✅ Works — App identity is sufficient      |
| S2       | User-to-server OAuth token (`ghu_…`) | ✅ Works — Requires user with Copilot seat |
| S3       | PAT with `copilot` scope             | ✅ Works — Current approach (baseline)     |

---

## Prerequisites

1. A registered GitHub App with the required permissions (see GitHub App plan §6: Permission manifest).
2. The App installed on a test repository.
3. The test repository must be a **private** repo under an organization with
   an active **GitHub Copilot Business** or **Enterprise** license.
4. An open issue in the test repo (note the issue number).
5. `CARETAKER_GITHUB_APP_PRIVATE_KEY` set to the App's downloaded PEM.
6. `CARETAKER_GITHUB_APP_ID` set to the App's numeric ID.
7. `CARETAKER_GITHUB_APP_INSTALLATION_ID` set to the installation's ID
   (visible at `https://github.com/organizations/{org}/settings/installations`).

---

## Step 1 — Mint a fresh installation token

```bash
# Install the optional github-app extras if not already present
pip install "caretaker[github-app]"

python - <<'EOF'
import asyncio, os
from caretaker.github_app.jwt_signer import AppJWTSigner
from caretaker.github_app.installation_tokens import InstallationTokenMinter

async def main():
    signer = AppJWTSigner(
        app_id=int(os.environ["CARETAKER_GITHUB_APP_ID"]),
        private_key_pem=os.environ["CARETAKER_GITHUB_APP_PRIVATE_KEY"],
    )
    async with InstallationTokenMinter(signer=signer) as minter:
        install_id = int(os.environ["CARETAKER_GITHUB_APP_INSTALLATION_ID"])
        token = await minter.get_token(install_id)
        print("installation token:", token.token[:8], "...", "expires_at:", token.expires_at)

asyncio.run(main())
EOF
```

Export the token:

```bash
export GHS_TOKEN="<token printed above>"
```

---

## Step 2 — Call the Copilot assignment endpoint (S1: App token)

```bash
OWNER="<your-org-or-user>"
REPO="<your-test-repo>"
ISSUE=<issue-number>

curl -X POST \
  -H "Authorization: Bearer $GHS_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/$OWNER/$REPO/issues/$ISSUE/copilot" \
  -w "\nHTTP %{http_code}\n"
```

**Expected responses:**

| HTTP code           | Meaning                                           | Next action               |
| ------------------- | ------------------------------------------------- | ------------------------- |
| `201 Created`       | **S1 confirmed** — installation token works       | Mark as resolved; skip S2 |
| `403 Forbidden`     | User identity required                            | Proceed to Step 3 (S2)    |
| `404 Not Found`     | Repo/issue not found, or App lacks `issues:write` | Check permissions         |
| `422 Unprocessable` | Copilot not enabled for the org                   | Check license             |

---

## Step 3 — Call with a user-to-server token (S2: OAuth token)

If Step 2 returns `403`, obtain a user-to-server OAuth token for a user who
has a Copilot seat using the GitHub App's OAuth flow:

```bash
# Replace with your App's OAuth client_id
CLIENT_ID="<CARETAKER_GITHUB_APP_CLIENT_ID>"

# 1. Get a device code
curl -X POST https://github.com/login/device/code \
  -H "Accept: application/json" \
  -d "client_id=$CLIENT_ID&scope=repo"
```

Follow the displayed URL + one-time code to authorize, then exchange the
`device_code` for a user token:

```bash
curl -X POST https://github.com/login/oauth/access_token \
  -H "Accept: application/json" \
  -d "client_id=$CLIENT_ID&device_code=<device_code>&grant_type=urn:ietf:params:oauth:grant-type:device_code"
```

Export the resulting `access_token` as `GHU_TOKEN` and re-run Step 2 with it.

---

## Step 4 — Record results and update the plan

Update the results in the GitHub App plan (§4)
with the scenario that succeeded.

If **S1 passes**:

- The `GitHubAppCredentialsProvider.copilot_token()` fallback path in
  `src/caretaker/github_app/provider.py`
  already handles this — no code change required.
- Close the spike; proceed to Phase 2 agent wiring.

If **only S2 passes**:

- Implement the full OAuth device-flow or web-flow in the
  `GET /oauth/callback` stub in
  `src/caretaker/mcp_backend/main.py`.
- Wire the resulting user token into `GitHubAppCredentialsProvider` via
  `user_token_supplier`.
- This is the more complex path; budget an additional 1–2 days.

---

## Acceptance criteria

- [ ] One of S1, S2, or S3 confirmed to work with a GitHub App identity.
- [ ] Result documented in `docs/github-app-plan.md §4`.
- [ ] If S1 passes: `COPILOT_PAT` can be deprecated.
- [ ] If only S2: OAuth callback implementation is scheduled.

---

## Related files

| File                                              | Role                                        |
| ------------------------------------------------- | ------------------------------------------- |
| `src/caretaker/github_app/jwt_signer.py`          | RS256 App JWT for API auth                  |
| `src/caretaker/github_app/installation_tokens.py` | Installation token minter                   |
| `src/caretaker/github_app/provider.py`            | `GitHubAppCredentialsProvider`              |
| `src/caretaker/mcp_backend/main.py`               | OAuth callback stub (`GET /oauth/callback`) |
| `docs/github-app-plan.md`                         | Full architectural plan                     |
| `docs/azure-mcp-architecture-plan.md`             | Multi-replica / Redis upgrade path          |
