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
