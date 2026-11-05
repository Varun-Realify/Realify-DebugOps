# Realify DebugOps

Autonomous AI debugging and QA platform. Architecture and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Status:** all phases (0–8) implemented. DebugOps finds bugs (GitHub issues plus automated scans), investigates them with AI agents, proposes fixes that the platform itself validates, and opens pull requests for humans to review and merge.

| Document | For |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | deploying on a server |
| [docs/GITHUB_APP.md](docs/GITHUB_APP.md) | connecting Realify's GitHub organization |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | how a debugging run works, and what "fixed" means |
| [docs/DETECTION.md](docs/DETECTION.md) | issue ingestion, scans, duplicate handling |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | monitoring, backup/restore, secret rotation, troubleshooting |
| [docs/SECURITY.md](docs/SECURITY.md) | threat model and controls |
| [docs/API.md](docs/API.md) | REST and WebSocket API reference (generated) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | design and decisions |
| [docs/AWS_DEVOPS_AGENT.md](docs/AWS_DEVOPS_AGENT.md) | AWS DevOps Agent (MCP) for production incidents: status, setup, security |

## Run with Docker Compose

Requirements: Docker 24+ with Compose v2.

```bash
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD (openssl rand -hex 24) and SECRET_KEY (openssl rand -hex 32).
# For plain-http local testing only, set COOKIE_SECURE=false.
chmod 600 .env

docker compose up -d --build
docker compose exec api python -m app.cli create-admin --email you@yourcompany.com --name "Your Name"
```

Open http://127.0.0.1:8088 (change with `WEB_BIND` / `WEB_PORT`).

Services: `web` (nginx + React), `api` (FastAPI, runs migrations on start), `worker` (scans, debugging runs, PR tracking), `llm-gateway` (the sandboxes' only route to the Claude API), `postgres` (not exposed to the host), `docker-proxy` (restricted Docker API, reachable only by the worker), `egress-proxy` (allowlist for sandbox internet access), `sandbox-image` (builds the scan sandbox image, then exits).

The first `up --build` takes several minutes, because the sandbox image bundles Semgrep, Gitleaks, OSV-Scanner, Python and Node.

**Production:** put a TLS-terminating reverse proxy (Caddy, nginx, a cloud load balancer) in front of `web`, keep `COOKIE_SECURE=true`, and bind `web` to localhost or a private interface.

## Try it: a live end-to-end test

This walk-through shows the whole flow on a real repository:

> **GitHub issue → AI investigates → finds root cause → fixes the code → runs the tests → opens a PR → a developer reviews and merges.**

You watch the AI work live in the browser. Allow about 30 minutes for the first time, most of it one-time setup.

### What you need

- DebugOps running (see [Run with Docker Compose](#run-with-docker-compose)) and an admin account.
- A GitHub account that can create apps and install them on the target repository.
- An **Anthropic API key** for the AI agents. Use a dedicated key with a spending limit.
- The repository to test on. The example below uses `Varun-Realify/Realify-DebugOps`; replace it with your own.

### Step 1. Create the GitHub App (one time, one click)

```bash
cd backend && uv sync
uv run python ../scripts/create_github_app.py --no-webhook --name "Realify DebugOps Test"
```

1. A browser page opens with every permission pre-filled. Click **Create GitHub App**. If GitHub says the name is taken, edit it on that page first.
2. The script saves the app's credentials into `.env` and `secrets/github-app.pem` by itself. Nothing needs copying by hand.
3. The success page shows an **install** link. Click it, choose **Only select repositories**, and pick the repository to test.

`--no-webhook` means GitHub does not need to reach your computer. You pull issues in with a button instead (Step 4). For automatic, real-time GitHub events, see [Optional: live GitHub events](#optional-live-github-events).

### Step 2. Add the AI key and restart

Add this line to `.env`. Keep the key private and never commit it.

```
ANTHROPIC_API_KEY=your-key
```

```bash
docker compose up -d
docker compose exec api python -m app.cli github-check
```

`github-check` should print `ok: authenticated as app '…'` and list your account under installations.

### Step 3. Connect the repository

1. Open http://127.0.0.1:8088 and sign in as an admin.
2. Go to **Repositories** and click **Sync from GitHub**. Your repository appears.
3. Click **Test access**. Every check should show ✓.
4. Tick **Enabled**.
5. Click **Settings** and set how the tests run, then **Save**.
   - Most Python or Node projects are detected automatically, so leave the commands empty.
   - For **this** repository, whose full test suite needs a database the sandbox does not have, use the commands below. Database tests fail the same way before and after a fix, so they do not affect the result; only the pure unit tests, including the AI's new test, decide it.
     - Install command: `python -m venv /workspace/.venv && /workspace/.venv/bin/pip install -q pytest`
     - Test command: `cd backend && /workspace/.venv/bin/python -m pytest --noconftest -q tests --junitxml=/workspace/reports/junit.xml`

### Step 4. Report a bug as a GitHub issue

Create an issue on GitHub with the label **bug**. A real example for this repository:

```bash
gh issue create -R Varun-Realify/Realify-DebugOps --label bug \
  --title "Short passwords are not redacted from logs and evidence" \
  --body "redact() in backend/app/detection/parsers.py only masks values of 6+ characters after password=/secret=/token=, so 'password=abc12' is stored in plain text. Expected: short values are redacted too."
```

Then in DebugOps, go to **Repositories** and click **Import issues**. The issue appears under **Bugs** within a few seconds.

You can also report a bug directly in DebugOps with **Bugs → Report bug**, including a stack trace and log files.

### Step 5. Watch the AI developer work

Open the bug and click **Investigate with AI**. The **Task Details** page opens:

| Part of the screen | What you see |
|---|---|
| **Stages** (left) | Each step ticks off as it completes: triage, code analysis, investigation, root cause, fix, regression test, validation, security, review, report, pull request |
| **Live feed** (centre) | Every file the agents read, every edit, every command and test run, and their reasoning, as it happens |
| **Code changes** tab | The diff of the fix and the new regression test |
| **Tests** tab | DebugOps' own proof: the new test **fails** on the original code, **passes** with the fix, and no other test breaks |
| **Security & review** tab | Security scan results and the independent reviewer's verdict |
| **Report** tab | Root cause, evidence (file and line), affected files, test and security results |

A run usually takes 5–15 minutes; the first is slower while dependencies install. The **Dashboard** shows every task and its live status.

### Step 6. Review and merge the fix

1. When validation passes, the AI opens a pull request. It appears under **Pull requests** in DebugOps, and on GitHub with the full report.
2. **A developer reviews and merges it on GitHub.** DebugOps never merges or deploys on its own.
3. In DebugOps, click **Refresh** on the task's PR panel to see the review, merge and CI status. With webhooks enabled, this happens automatically.
4. After the merge, your normal CI/CD deploys it. DebugOps records the deployment and can watch production health (configured in repository **Settings**).

### If something does not work

- A failed stage shows in **red** in the live feed with the reason.
- **System errors** (sidebar, admin) lists everything that went wrong.
- Full logs: `docker compose logs -f worker api`.
- When the AI cannot prove a fix (for example, the tests cannot run), the task ends as **Needs human** with a report explaining why. It never opens an unverified PR.

### Optional: live GitHub events

With `--no-webhook` you use **Import issues** and **Refresh**. For new issues and PR updates to arrive automatically, GitHub must be able to reach DebugOps:

- **Server:** run DebugOps on a server with an HTTPS address. Create the app with `--webhook-url https://your-debugops-host` instead of `--no-webhook`.
- **Laptop, temporary:** run `cloudflared tunnel --url http://127.0.0.1:8088`. This exposes the local DebugOps to the internet, so do it only while testing. In the GitHub App's settings, set the webhook URL to `https://<tunnel-url>/api/v1/github/webhook` and tick **Active**.

## Roles

| | Admin | Developer | Reviewer |
|---|---|---|---|
| Dashboard | ✓ | ✓ | ✓ |
| View repositories, browse code of enabled repos | ✓ | ✓ | ✓ |
| View bugs, scans, test results | ✓ | ✓ | ✓ |
| Report bugs (with logs), start scans, import issues, triage bugs, open GitHub issues | ✓ | ✓ | |
| Start / cancel AI debugging runs | ✓ | ✓ | |
| Approve or reject PR creation when a repository enables that gate (not for runs you started) | ✓ | | ✓ |
| View system errors | ✓ | | |
| Export the run audit trail (CSV) | ✓ | ✓ | ✓ |
| Manage users, view audit log | ✓ | | |
| Sync GitHub, enable repos, test access, repository scan settings | ✓ | | |

More permissions arrive with later phases (see the RBAC matrix in the architecture doc).

## Security behavior

- Passwords hashed with argon2id, minimum 12 characters.
- Server-side sessions (only a SHA-256 of the token is stored). Cookies are HttpOnly, SameSite=Lax, and Secure.
- Sessions expire after 120 minutes idle or 12 hours total (configurable).
- Every state-changing request needs a session-bound CSRF token, and cross-origin state-changing requests are rejected.
- An account locks for 15 minutes after 5 failed logins. Every login failure returns the same response, so attackers can't tell which emails have accounts.
- Changing a password, role, or active status signs that user out everywhere.
- The last active admin cannot be demoted or disabled.
- The audit log is append-only: a database trigger blocks UPDATE and DELETE.

## Development

```bash
# Test database (any Postgres 16 works; set TEST_DATABASE_URL to override)
docker run -d --name debugops-testdb -e POSTGRES_USER=debugops -e POSTGRES_PASSWORD=debugops \
  -e POSTGRES_DB=debugops -p 127.0.0.1:55432:5432 postgres:16-alpine

cd backend && uv sync && uv run pytest          # backend tests (real Postgres)
cd frontend && npm ci && npm run build          # typecheck + build
```

Run locally without Docker:

```bash
cd backend
export DATABASE_URL=postgresql+asyncpg://debugops:debugops@127.0.0.1:55432/debugops SECRET_KEY=$(openssl rand -hex 32) COOKIE_SECURE=false
uv run alembic upgrade head
uv run uvicorn app.main:app --port 8001 &
uv run python -m app.worker &
cd ../frontend && npm run dev                    # http://localhost:5174, proxies /api to :8001
```

New migration: `uv run alembic revision --autogenerate -m "..."`, review the file, then `uv run alembic check`.

## Backup (interim; full guide in Phase 8)

```bash
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > debugops-$(date +%F).dump
# restore into an empty database:
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < debugops-YYYY-MM-DD.dump
```

## Troubleshooting

- **Dashboard says "No worker has checked in":** run `docker compose logs worker`.
- **Login works but every action returns 403 over plain http:** with `COOKIE_SECURE=true`, browsers drop the cookies on non-HTTPS, non-localhost origins. Use HTTPS.
- **Locked out:** another admin can reset your password, which also unlocks the account. If you're the only admin, create a new admin with the CLI.
