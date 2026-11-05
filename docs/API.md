# API Reference

Base path `/api/v1`. Generated from the code by `scripts/gen_api_docs.py` — do not edit by hand.

**Authentication:** session cookie from `POST /api/v1/auth/login`. Every state-changing request must send the `X-CSRF-Token` header with the value of the `debugops_csrf` cookie. Cross-origin state changes are rejected.

**Roles:** endpoints marked _Admin_, _Developer+_ (admin, developer) or _Approver_ (admin, reviewer); everything else needs any signed-in user. Public: `POST /auth/login`, `POST /github/webhook` (HMAC-signed), `/api/health/*`.

**Live updates:** `WS /api/v1/ws?after=<event id>&run_id=<optional>` (session cookie; same-origin only). Messages: `{kind: event|run|scan|ready|ping, ...}`.

## health

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health/live` | Live |
| `GET` | `/api/health/ready` | Ready |

## dashboard

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/activity` | Activity — Activity / error log (level=error for the System errors view). |
| `GET` | `/api/v1/dashboard/summary` | Summary |

## users

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/audit-logs` | Audit Logs |
| `GET` | `/api/v1/users` | List Users |
| `POST` | `/api/v1/users` | Create User |
| `PATCH` | `/api/v1/users/{user_id}` | Update User |
| `DELETE` | `/api/v1/users/{user_id}` | Deactivate User — Soft delete: deactivates and revokes sessions; audit history keeps the user reference. |

## auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/auth/change-password` | Change Password |
| `POST` | `/api/v1/auth/login` | Login |
| `POST` | `/api/v1/auth/logout` | Logout |
| `GET` | `/api/v1/auth/me` | Me |

## alerts

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/aws/sns` | Sns Endpoint |

## detection

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/bugs` | List Bugs |
| `GET` | `/api/v1/bugs/{bug_id}` | Get Bug |
| `PATCH` | `/api/v1/bugs/{bug_id}` | Update Bug |
| `POST` | `/api/v1/bugs/{bug_id}/github-issue` | Open Github Issue |
| `POST` | `/api/v1/bugs/{bug_id}/mark-duplicate` | Mark Duplicate |
| `POST` | `/api/v1/repositories/{repo_id}/bugs` | Report Bug |
| `POST` | `/api/v1/repositories/{repo_id}/import-issues` | Import Issues |
| `POST` | `/api/v1/repositories/{repo_id}/scans` | Start Scan |
| `GET` | `/api/v1/scans` | List Scans |
| `GET` | `/api/v1/scans/{scan_id}` | Get Scan |
| `GET` | `/api/v1/scans/{scan_id}/tests` | Scan Tests |

## runs

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/bugs/{bug_id}/investigate` | Investigate |
| `GET` | `/api/v1/pull-requests` | List Prs — Proposed fixes: DebugOps PRs (and runs waiting for the optional pre-PR approval) with their review state. |
| `POST` | `/api/v1/pull-requests/{pr_id}/refresh` | Refresh Pr |
| `GET` | `/api/v1/runs` | List Runs |
| `GET` | `/api/v1/runs/export.csv` | Export Runs — Audit trail of debugging runs as CSV (one row per task). |
| `GET` | `/api/v1/runs/{run_id}` | Get Run |
| `POST` | `/api/v1/runs/{run_id}/approve` | Approve |
| `GET` | `/api/v1/runs/{run_id}/attempts/{attempt_id}/diff` | Attempt Diff |
| `POST` | `/api/v1/runs/{run_id}/cancel` | Cancel |
| `GET` | `/api/v1/runs/{run_id}/events` | Run Events |
| `POST` | `/api/v1/runs/{run_id}/reject` | Reject |
| `GET` | `/api/v1/runs/{run_id}/tests` | Run Tests |

## github

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/github/installations` | List Installations |
| `GET` | `/api/v1/github/status` | Github Status |
| `POST` | `/api/v1/github/sync` | Sync Installations |
| `POST` | `/api/v1/github/webhook` | Github Webhook |
| `GET` | `/api/v1/repositories` | List Repositories |
| `PATCH` | `/api/v1/repositories/{repo_id}` | Update Repository |
| `GET` | `/api/v1/repositories/{repo_id}/contents` | Repository Contents |
| `POST` | `/api/v1/repositories/{repo_id}/test-access` | Test Access — Mint a token scoped to only this repository and exercise each read permission. |

## integrations

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v1/integrations/aws-devops-agent` | Devops Agent Status |
| `PATCH` | `/api/v1/integrations/aws-devops-agent` | Devops Agent Enable |
| `POST` | `/api/v1/integrations/aws-devops-agent/check` | Devops Agent Check — Live connectivity test (MCP initialize + tools/list) against the configured endpoint. Never returns the token. |
