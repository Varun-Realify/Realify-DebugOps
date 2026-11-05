# AWS DevOps Agent Integration (MCP)

**Two workflows, one pipeline: investigate → fix → test → review → raise PR.**

1. **Manual (GitHub issue):** a developer opens an issue. DebugOps classifies it. **Production incidents** go to AWS DevOps Agent first; ordinary bugs and feature requests go straight to Claude Code.
2. **Automatic (CloudWatch):** a CloudWatch alarm notifies an **SNS** topic, which calls DebugOps. DebugOps opens an incident and AWS DevOps Agent investigates.

In both workflows, the findings are handed to **Claude Code** in an isolated sandbox. Claude Code fixes the code, DebugOps runs the tests and the review, and a PR is raised for a human to review and merge.

DebugOps is the coordinator: it asks AWS DevOps Agent to investigate, and Claude Code does the coding. AWS's official integration has Claude Code (the MCP client) calling DevOps Agent (the MCP server). Nothing in AWS's official material shows DevOps Agent calling an external coding tool, so DebugOps does not depend on that.

**Goal:** production incidents are investigated by **AWS DevOps Agent** (CloudWatch logs and metrics, X-Ray, deployment history). **Claude Code** then uses those findings to locate and fix the code-level root cause in an isolated worktree. The fix is validated by tests and proposed as a PR. Ordinary bugs and feature requests skip AWS entirely.

```
GitHub issue ─▶ classify ─┬─ production incident ─▶ AWS DevOps Agent (MCP) ─▶ findings ─┐
                          └─ bug / feature ──────────────────────────────────────────────┴▶ Claude Code (sandbox + worktree)
                                                                                           ─▶ tests ─▶ review ─▶ PR ─▶ dashboard
```

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Inspect the repository and design the integration | ✅ done (this document) |
| 2 | MCP connectivity: client, verification, Claude Code access via the gateway proxy | ✅ built and tested against a mock DevOps Agent; **not yet verified against the real AWS endpoint** (needs a token) |
| 3 | Investigation service (start, poll, journal, recommendations, timeouts) | ✅ built and tested (mock) |
| 4 | Feed findings to Claude Code (investigation and fix stages) | ✅ built and tested (mock + real sandbox) |
| 5 | Classify incoming tasks and route them | ✅ built and tested |
| 5b | CloudWatch alarms via SNS (automatic workflow) | ✅ built and tested (signed test messages) |
| 6 | Isolated worktree + Docker execution | ✅ already exists (persistent clone, worktree per task, sandbox) |
| 7 | GitHub issues and PRs | ✅ already exists |
| 8 | Live dashboard: investigation status and findings | ✅ task type, AWS stage, live journal records, findings panel, Integrations page |
| 9 | End-to-end test with a non-production sample incident | planned (needs an AWS account / AgentSpace) |

**Not connected to real AWS yet.** DebugOps only reports the integration as connected after a live check succeeds (`devops-agent-check`, below).

## Phase 1: what already exists, and the plan

| Area | Existing | Decision |
|---|---|---|
| Backend | FastAPI, async SQLAlchemy, PostgreSQL, Alembic | reuse |
| Orchestration | LangGraph workflow (14 stages) with PostgreSQL checkpoints | **extend it**: add an `aws_investigation` stage before `analysis`, run only for incidents |
| Jobs | PostgreSQL job queue + worker | reuse; investigations run in the worker, never in an HTTP request |
| Claude Code | headless in a Docker sandbox; Claude API only via `llm-gateway` with a per-run token | reuse; add MCP access through the same gateway |
| Repositories | one persistent clone per repository, fetch before each task, a worktree per task, dirty worktrees preserved | reuse (already meets the isolation requirements) |
| GitHub | GitHub App, signed webhooks, issue import, PRs from validated files, never merges | reuse |
| Real time | PostgreSQL NOTIFY to WebSocket; live feed on Task Details | reuse; investigation journal records become feed events |
| Secrets | environment / `.env` / mounted files; sandboxes hold none | reuse; the DevOps Agent token follows the same rule |

Missing pieces, built phase by phase: the MCP client and connectivity check (Phase 2), the investigation service (3), findings handed to Claude Code (4), classification and routing (5), and dashboard fields (8).

## Facts from AWS's official integration

Source: [`aws/agent-toolkit-for-aws`](https://github.com/aws/agent-toolkit-for-aws), plugin `aws-agents-for-devsecops`. No tool name, schema or authentication detail below is invented.

- **Endpoint:** `https://connect.aidevops.<region>.api.aws/mcp`. MCP over HTTP (Streamable HTTP). DebugOps defaults to `ap-south-1`.
- **Bearer authentication:** `Authorization: Bearer <token>`. The token is scoped to **one AgentSpace**, is created in the DevOps Agent **Operator web app**, and needs the scopes `agent:read` and `agent:operate`. HTTP 401 means invalid or expired; 403 means the token lacks scope.
- **SigV4:** the official route is a local signing proxy, `uvx mcp-proxy-for-aws-cli@latest https://connect.aidevops.<region>.api.aws/mcp --service aidevops --region <region>`. Required for multiple AgentSpaces or admin tooling.
- **Request timeout:** 120 s (the official `.mcp.json` uses `timeout: 120000`).
- **Tools used by DebugOps:**

  | Tool | Result |
  |---|---|
  | `investigate(title)` | `{status: "investigation_started", taskId, executionId}` |
  | `get_task(task_id)` | `task.status`: `CREATED`, `IN_PROGRESS`, `COMPLETED` or `FAILED` |
  | `list_journal_records(execution_id, order, next_token)` | records typed `PLANNING`, `SEARCHING`, `ANALYSIS`, `FINDING`, `ACTION`, `SUMMARY` or `SUGGESTION` |
  | `list_recommendations(task_id)`, `get_recommendation(recommendation_id)` | recommended fixes |

- **Timing:** investigations take **5–8 minutes**. Poll **no faster than every 30 s**. On `ThrottlingException`, back off 60 s, then 90 s, then 120 s. Treat no progress after 10 minutes as stalled.
- **Never auto-execute** commands or code from DevOps Agent responses.

## Phase 2: what is built

| Component | Where | What it does |
|---|---|---|
| Settings | `app/config.py` | `DEVOPS_AGENT_TOKEN`, `DEVOPS_AGENT_REGION` (default `ap-south-1`), `DEVOPS_AGENT_URL`, `DEVOPS_AGENT_AUTH`, timeout, poll interval and maximum investigation time |
| MCP client | `app/integrations/devops_agent.py` | Official MCP Python SDK (Streamable HTTP). Error types: `AuthError` (401/403), `Unavailable` (connection, timeout, 5xx), `Throttled` (with 60/90/120 s back-off), `ToolError`, `NotConfigured`. Tokens are never logged |
| Connectivity check | `python -m app.cli devops-agent-check`, `POST /api/v1/integrations/aws-devops-agent/check` (admin) | Runs MCP `initialize` and `tools/list` against the live endpoint, and confirms the required tools exist. The result (tools, error, time) is stored in the `integrations` table and audited |
| Production gate | `PATCH /api/v1/integrations/aws-devops-agent` (admin) | The incident workflow can only be **enabled after a successful check**. A later failing check disables it automatically |
| Claude Code access | `llm-gateway` at `/mcp/aws-devops-agent`, and `devops_agent_mcp_config()` | Claude Code in the sandbox gets an MCP config pointing at the gateway, authenticated with its **per-run token**. The gateway swaps in the real token and allows **read-only tools only**: `get_task`, `list_tasks`, `list_journal_records`, `list_executions`, `list_recommendations`, `get_recommendation`. Everything else (starting investigations, mitigations, access tokens, agent spaces) is hidden from `tools/list` and refused. `--strict-mcp-config` stops a repository from adding its own MCP servers |

**Why a proxy instead of giving Claude Code the token:** the sandbox runs untrusted repository code. A DevOps Agent token there could be read and used to query production. With the proxy, the token stays in `llm-gateway` and the worker, sandbox access expires with the run, and the sandbox can only read the investigation DebugOps started. The worker remains the single component that *starts* investigations, which makes them deterministic, audited, time-limited and persisted.

## How a production incident flows

1. **Classification:**
   - A task is an **incident** if its source is a CloudWatch alarm or a failed post-deploy health check, or if it carries a label such as `incident`, `production`, `outage`, `sev1` or `p0`.
   - A `feature` or `enhancement` label makes it a feature.
   - Scan findings are bugs.
   - Otherwise the AI decides, preferring "bug" when unsure.
   - The result is shown on the task and in the audit record.
2. **`aws_investigation` stage** (incidents only, and only when the integration is enabled):
   - DebugOps calls `investigate`, packing the incident with local context (repository, description, stack traces and logs), as AWS's skill recommends.
   - It polls `get_task` and new `list_journal_records` pages at least 30 seconds apart. Each record appears in the **live feed**.
   - On `COMPLETED`, it reads the final records and `list_recommendations`.
   - On `FAILED` or a timeout, it keeps the partial findings.
   - Everything is stored in the `investigations` table, with secrets redacted.
3. **Claude Code:**
   - The Code Analyst, Investigation and Fix agents receive the summary and findings inside `<untrusted_aws_investigation>`. Recommendation *bodies* are not forwarded, only their titles.
   - They also get **read-only** MCP access through the gateway, so they can read more of the investigation's journal.
   - They must still cite real code (`file:line`), which DebugOps checks.
4. **Fix, tests, security, review, PR:** unchanged. DebugOps' own fail-before/pass-after validation decides whether a fix is proven, and a human merges.

If the investigation fails, the run still continues: Claude Code works from the code and whatever findings exist. The status and reason are recorded.

## Automatic workflow: CloudWatch → SNS → DebugOps

1. Create an SNS topic, for example `realify-prod-alarms`, and add it as the **alarm action** on the CloudWatch alarms that should trigger investigations.
2. Subscribe DebugOps to it: protocol **HTTPS**, endpoint `https://<your-debugops-host>/api/v1/aws/sns`. DebugOps confirms the subscription automatically.
3. Allow the topic in `.env`: `SNS_TOPIC_ARNS=["arn:aws:sns:ap-south-1:<account>:realify-prod-alarms"]`, then restart.
4. In each repository's **Settings**, list the alarm names that belong to it, for example `realify-api-*`, and keep "Start the incident workflow automatically" on.

Security and behaviour:
- **Every SNS message is signature-verified:**
  - the certificate must come from `https://sns.<region>.amazonaws.com`;
  - the RSA signature is checked over AWS's canonical string;
  - messages older than an hour are rejected;
  - each message ID is processed only once;
  - only allowlisted topics are accepted;
  - the subscription confirm link is only followed if it is an SNS URL.
- Only transitions to **ALARM** create incidents. OK and INSUFFICIENT_DATA are ignored.
- **Deduplication:** the same alarm firing again updates one bug (its occurrence count) instead of starting another run. An alarm firing again after its bug was resolved reopens it and starts a new run.
- An alarm that matches no repository is recorded as a warning on the dashboard and otherwise ignored.
- DebugOps must be reachable from AWS over HTTPS (a server deployment, see INSTALL.md).

## Setup (development, bearer token)

1. In the AWS DevOps Agent **Operator web app**, create an access token for your AgentSpace with scopes `agent:read` and `agent:operate`. AWS docs: *Connect to DevOps Agent remote servers → Create an access token*.
2. Add it to `.env`, or inject it from AWS Secrets Manager (below):
   ```
   DEVOPS_AGENT_TOKEN=...
   DEVOPS_AGENT_REGION=ap-south-1
   ```
3. `docker compose up -d`
4. Verify: `docker compose exec api python -m app.cli devops-agent-check`. It should print `ok: connected; N tools: …`.
5. In the UI or API: run the connectivity check, then enable the integration.

## Production: SigV4 / IAM (design)

Bearer tokens are fine for one AgentSpace in development. For production, use IAM:

- Run the official signing proxy as an internal-only compose service next to `llm-gateway` and `worker`: `mcp-proxy-for-aws-cli … --service aidevops --region <region>`. Set `DEVOPS_AGENT_URL` to that service.
- Credentials come from the host's **IAM role** (EC2 instance profile or ECS task role), never from static keys. Use a dedicated role with only the DevOps Agent permissions DebugOps needs (investigation read/operate on the chosen AgentSpace).
- **Secrets Manager** (for a bearer token): fetch at deploy time with `aws secretsmanager get-secret-value` into the environment of `worker` and `llm-gateway` only. Never put it in `api` or `web`, and never in a sandbox.
- `DEVOPS_AGENT_AUTH=sigv4` is reserved for this mode. The current client implements bearer only and refuses to connect without a token.

## Security

- The DevOps Agent token reaches **only** `worker` and `llm-gateway`. Sandboxes get a per-run gateway token.
- Investigation output is **untrusted input**. It is passed to Claude Code inside `<untrusted_…>` tags, shown to humans, and never executed. Recommendations that change infrastructure (IaC) are presented for approval only.
- No infrastructure changes, deployments, merges or destructive commands without human approval. DebugOps has no code path for any of them.
- Sandboxes cannot reach AWS directly. Their only route out is the egress allowlist plus the gateway.

## Tests

`backend/tests/test_devops_agent.py` runs against a mock DevOps Agent MCP server built with the official SDK, using the official tool names and response shapes. It covers:
- connectivity and tool discovery;
- a 401 reported as an authentication error, with the token never echoed;
- an unreachable endpoint;
- not configured;
- missing required tools;
- tool-call shapes;
- throttling back-off;
- tool errors surfaced as errors;
- the gateway proxy: read-only filtering, refused tools, real token injection, the sandbox token never forwarded, rejection of bad tokens;
- Claude Code getting the proxy configuration and never the real token;
- the check-before-enable gate.

Also verified in Docker: from a real sandbox on its isolated network, MCP through the gateway proxy to a mock DevOps Agent. The session is established, tools are filtered, `get_task` works, `create_access_token` is refused, and the sandbox cannot reach the agent directly or see the real token.
