# Operations

## Services

| Service | Role | State |
|---|---|---|
| `web` | nginx: dashboard + reverse proxy to the api | stateless |
| `api` | REST, WebSocket, webhooks, auth; runs DB migrations on start | stateless |
| `worker` | scans, debugging runs (LangGraph), PR tracking, scheduler | stateless (runs resume from DB checkpoints) |
| `llm-gateway` | sandbox-facing proxy to the Claude API, per-run budgets | stateless |
| `postgres` | all data: users, audit log, bugs, runs, checkpoints, job queue | **stateful: back it up** |
| `docker-proxy` | restricted Docker API for the worker | stateless |
| `egress-proxy` | domain allowlist for sandboxes | stateless |
| `repo-cache` volume | persistent clone per repository + per-task worktrees | rebuildable from GitHub (no backup needed) |
| sandbox containers | one per scan/run, removed afterwards | ephemeral |

## Monitoring

- **Health:** `GET /api/health/live` (process up) and `GET /api/health/ready` (`{"status":"ok","workers_online":N}`). Alert when `ready` fails or `workers_online` is 0.
- **Dashboard:** live agent status, worker list, failed scans and runs.
- **Logs:** `docker compose logs -f api worker llm-gateway`. Every job failure, API 500 and production regression is also an `error` event on the dashboard (Admin, then **System errors**).
- **Error tracking (optional):** set `SENTRY_DSN` (Sentry or a self-hosted GlitchTip) to receive exceptions with stack traces from api, worker and llm-gateway. No request bodies or cookies are sent.

### Centralized logs

Every service writes **JSON lines** to stdout (`ts`, `level`, `service`, `logger`, `msg`, `request_id`, `exc`). API responses carry an `X-Request-ID` header that matches the log lines and the error event, so a user-reported error can be traced. Logs rotate locally (20 MB × 5 per container). To ship them centrally, change the driver in the `x-logging` block of `docker-compose.yml`:

```yaml
x-logging: &logging
  logging:
    driver: loki            # or fluentd, awslogs, gcplogs, splunk ...
    options: {loki-url: "https://loki.internal/loki/api/v1/push"}
```

Alternatively, leave `json-file` and run a collector on the host (Promtail, Fluent Bit, Vector, Datadog agent) that tails Docker's container logs.
- **Audit log:** Admin, then Audit log. Logins, user changes, approvals, scans, PR and issue creation, and settings changes. The table is append-only (a database trigger blocks UPDATE and DELETE).
- **AI spend:** each run shows tokens and estimated cost. The gateway hard-limits each run (`RUN_MAX_LLM_REQUESTS`, `RUN_MAX_LLM_TOKENS`). Also set a spend limit on the Anthropic workspace itself.

## Backup and recovery

Everything lives in PostgreSQL. Back it up daily, and before every upgrade.

```bash
scripts/backup.sh                         # writes backups/debugops-YYYYmmdd-HHMMSS.dump (mode 600)
scripts/restore.sh backups/<file>.dump    # DESTRUCTIVE: replaces current data; asks for confirmation
```

- **Schedule:** cron on the host, for example `0 2 * * * cd /opt/debugops && scripts/backup.sh && find backups -mtime +30 -delete`.
- **Off-host copies:** encrypt and copy dumps off the host (for example `age` or `gpg`, then object storage). Dumps contain user password hashes, bug evidence (secrets already redacted) and audit history.
- **Restore drill:** verified during Phase 8 on a live stack: backup, change data, restore, then confirm the change was rolled back and the triggers survived. Repeat it quarterly.
- **What is not in the database:** `.env` and `secrets/github-app.pem`. Keep them in your secret manager. If they are lost, regenerate the GitHub App private key and the Anthropic key.
- **RPO/RTO:** with daily backups, RPO is 24 h. A restore takes minutes for typical sizes.
- **After a restore,** runs that were in progress resume or fail cleanly. Click **Sync from GitHub** to reconcile installations.

## Routine tasks

| Task | How |
|---|---|
| Add a user / change a role | Admin, then Users (role changes sign the user out) |
| Locked-out user | Admin resets the password (this also unlocks the account) |
| Lost the only admin | `docker compose exec api python -m app.cli create-admin --email ...` |
| Add a repository | Install the App on it in GitHub, Sync, Test access, Enable |
| Allow a sandbox domain (private registry) | edit `egress-proxy/allowlist.txt`, then `docker compose up -d --build egress-proxy` |
| Update Semgrep rules / scanner versions | edit the ARGs in `sandbox/Dockerfile` (pin new sha256 values), then `docker compose build sandbox-image` |
| More throughput | `docker compose up -d --scale worker=3` (each worker runs one scan or run at a time) |
| Cancel a runaway run | Run page, then **Cancel run** (stops after the current step) |

## Secret rotation / incident response

| Secret | Rotate | Effect |
|---|---|---|
| `SECRET_KEY` | change in `.env`, `docker compose up -d api` | invalidates CSRF tokens; users sign in again |
| Session compromise | Admin disables the user, or the user changes their password | all of that user's sessions are revoked immediately |
| GitHub App private key | generate a new key in the App settings, replace `secrets/github-app.pem`, delete the old key in GitHub | installation tokens are short-lived (1 h), so old ones expire on their own |
| Webhook secret | update GitHub App settings and `.env` together | deliveries fail with 401 in between; redeliver them afterwards |
| Anthropic key | create a new key, update `.env`, `docker compose up -d worker llm-gateway`, revoke the old key | sandboxes never had the key; per-run gateway tokens are revoked when a run's sandbox is released |
| Suspected sandbox escape | stop the stack, rotate every secret above, rebuild the host | see SECURITY.md |

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard: "No worker has checked in" | `docker compose logs worker`. Is Docker reachable? (`docker compose logs docker-proxy`) |
| Scans fail at `sandbox` | `docker image ls debugops-sandbox` (rebuild with `docker compose build sandbox-image`); look for `network debugops-sandbox not found` (run `docker compose up -d`) |
| Scan step `tests` failed / suite not detected | set `install_command` / `test_command` in repository settings. Dependency downloads blocked: add the domain to the egress allowlist |
| `osv` step fails | egress to `api.osv.dev` (egress-proxy logs show denied domains) |
| Investigate button returns 503 | `ANTHROPIC_API_KEY` or the GitHub App is not configured |
| Run fails at an agent step with "token budget exhausted" | raise `RUN_MAX_LLM_TOKENS` / `RUN_MAX_LLM_REQUESTS`, or narrow the bug report |
| Run ends `needs_human` with verdict `unverifiable` | the test suite gives no per-test results. Configure it to write JUnit XML to `/workspace/reports/junit.xml` |
| Approve button missing | only Reviewers and Admins approve, and never the user who started the run |
| PR not opened: "lacks write permission" | grant Contents / Pull requests *Read and write* to the App (an org owner must accept) |
| Scan or run fails with `git fetch failed` | Check repository access (Repositories, then **Test access**) and egress from the worker to github.com. A corrupted clone can be deleted: `docker compose exec worker rm -rf /var/lib/debugops/repos/<id>.git`; it is re-created on the next task |
| A worktree was left behind ("uncommitted changes") | DebugOps never discards uncommitted work. Inspect it in `/var/lib/debugops/repos/worktrees/<repo id>/<task>`, then remove it with `git worktree remove` |
| Run ends `needs_human`: "branch … already exists … refusing to overwrite" | Someone else's branch already has that name on GitHub. Rename or delete it, then start a new run |
| Live dashboard says "reconnecting" | the reverse proxy must allow WebSocket upgrades on `/api/v1/ws` |
| Login says "Too many login attempts" | the per-IP limit (`LOGIN_RATE_PER_MINUTE`). Wait a minute |
| Webhook deliveries 401 | webhook secret mismatch. See GITHUB_APP.md |
