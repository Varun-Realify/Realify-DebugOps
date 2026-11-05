# GitHub App Setup

DebugOps reaches private repositories through a **GitHub App** that belongs to Realify's GitHub organization, not to any one person. Access is granted per repository, and every token is short-lived.

Time needed: about 10 minutes. You need to be an owner of the GitHub organization.

## 1. Decide the public URL

GitHub must be able to reach `https://<your-debugops-host>/api/v1/github/webhook`.

- **Production:** the HTTPS hostname of your DebugOps server, for example `https://debugops.realify.ai`.
- **Local testing:** use a tunnel to `http://127.0.0.1:8088`, such as `cloudflared tunnel --url http://127.0.0.1:8088` or `npx smee-client`. Never expose a dev instance permanently.

## 2. Create the App

GitHub → your organization → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | `Realify DebugOps` (must be globally unique; the URL slug is derived from it) |
| Homepage URL | your DebugOps URL |
| Callback URL / Setup URL | leave empty |
| Webhook → Active | ✓ |
| Webhook URL | `https://<host>/api/v1/github/webhook` |
| Webhook secret | output of `openssl rand -hex 32` (keep it for step 4) |
| SSL verification | Enabled |
| Where can this app be installed? | **Only on this account** |

### Repository permissions (Phase 2: read-only)

| Permission | Access | Used for |
|---|---|---|
| Metadata | Read-only | mandatory; repository info |
| Contents | Read-only | read code, clone (later phases) |
| Issues | Read-only | ingest bug reports (Phase 3) |
| Pull requests | Read-only | track fix PRs (Phase 6) |
| Checks | Read-only | read CI results on the PR and on the merge commit |
| Deployments | Read-only | observe deployments made by your CD after merge (DebugOps never deploys) |

Leave every other permission at **No access**, including Organization and Account permissions.

**Write access, for when you are ready for DebugOps to propose fixes:**

| Permission | Access | Used for |
|---|---|---|
| Contents | Read and write | create the `debugops/run-N` branch and its commit (never pushes to existing branches) |
| Pull requests | Read and write | open the fix PR |
| Issues | Read and write | open issues for detected bugs (optional), comment on the originating issue |

**Never grant Administration.** DebugOps has no merge capability. Recommended: a branch ruleset on each repository that blocks the App from pushing to anything except `debugops/*`. GitHub then asks an org owner to approve the change, and DebugOps records the new permissions automatically.

### Subscribe to events

`Issues`, `Issue comment`, `Pull request`, `Pull request review`, `Push`, `Check run`, `Check suite`, `Deployment`, `Deployment status`, `Repository`.

(Installation events are always sent.)

Click **Create GitHub App**.

## 3. Collect credentials

On the App's page:

1. **App ID** is shown near the top. Also note the **slug** in the public page URL, `https://github.com/apps/<slug>`.
2. **Private keys → Generate a private key.** A `.pem` file downloads.

Store the key on the DebugOps server only:

```bash
mv ~/Downloads/realify-debugops.*.private-key.pem ./secrets/github-app.pem
chmod 600 ./secrets/github-app.pem
```

`./secrets` is git-ignored and mounted **read-only** into the `api` and `worker` containers. The key never reaches the browser, the frontend bundle, logs, or (in later phases) the AI agent sandboxes.

## 4. Configure DebugOps

In `.env`:

```bash
GITHUB_APP_ID=123456
GITHUB_APP_SLUG=realify-debugops
GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app.pem
GITHUB_WEBHOOK_SECRET=<the value from step 2>
```

Then apply and verify:

```bash
docker compose up -d
docker compose exec api python -m app.cli github-check
# ok: authenticated as app 'realify-debugops' (id=123456, owner=realify)
```

## 5. Install on repositories

1. In DebugOps, go to **Repositories → Install on GitHub**, or open `https://github.com/apps/<slug>/installations/new`.
2. Choose the organization, then **Only select repositories**, and pick the repositories DebugOps may see. Prefer this over "All repositories".
3. DebugOps receives the `installation` webhook and lists those repositories. If webhooks can't reach you yet, click **Sync from GitHub**.
4. For each repository, an admin clicks **Test access**. This mints a token scoped to *only that repository* with read permissions and reports each check.
5. Tick **Enabled** to let DebugOps work on it. Repositories are disabled by default.

To revoke access: remove the repository from the installation on GitHub (DebugOps disables it automatically), or uninstall the App entirely.

## Security model

- **App JWT:** valid 9 minutes, signed with the private key, used only to mint installation tokens.
- **Installation tokens:** valid 1 hour, requested with an explicit `repository_ids` list and the minimum permissions for the call. A token for repository A cannot read repository B.
- **Webhooks:** every delivery is verified with HMAC-SHA256 (`X-Hub-Signature-256`) against the webhook secret, using constant-time comparison. Unsigned or mis-signed requests are rejected with 401 and nothing is stored. Each `X-GitHub-Delivery` ID is processed once (replay protection). A delivery that failed can be retried with GitHub's **Redeliver** button.
- **Repository paths** from users are validated: no `..`, absolute paths, control characters or URL metacharacters.
- **Pagination** never follows links to a host other than the configured GitHub API, so tokens can't leak.
- **No merge capability:** the App is never granted Administration permission, and DebugOps contains no merge code.

## GitHub Enterprise Server

```bash
GITHUB_API_URL=https://github.yourcompany.com/api/v3
GITHUB_WEB_URL=https://github.yourcompany.com
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `github-check`: `cannot load GitHub App private key` | Wrong path or unreadable file. Check `./secrets/github-app.pem` exists and `GITHUB_APP_PRIVATE_KEY_FILE` points to `/run/secrets/github-app.pem`. |
| `GitHub API 401: A JSON web token could not be decoded` | The key doesn't belong to this App ID, or the server clock is off by more than a minute (check NTP). |
| GitHub shows webhook deliveries failing with 401 | Webhook secret mismatch between GitHub and `.env`. |
| Deliveries fail with 503 | `GITHUB_WEBHOOK_SECRET` is not set. |
| Repository shows **Removed** | It was removed from the installation on GitHub. Re-add it there, then Sync. |
| Test access: a check shows "permission not granted" | Add that permission in the App settings; an org owner must accept the change. |
| `rate limited until …` | GitHub API quota hit (5,000+/hour per installation). Wait for the reset time shown. |
