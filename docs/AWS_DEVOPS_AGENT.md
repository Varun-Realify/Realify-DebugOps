# AWS DevOps Agent Integration (MCP)

**Two workflows, one pipeline: investigate → fix → test → review → raise PR.**

1. **Manual (GitHub issue):** a developer opens an issue. DebugOps classifies it. **Production incidents** go to AWS DevOps Agent first; ordinary bugs and feature requests go straight to Claude Code.
2. **Automatic (CloudWatch):** a CloudWatch alarm notifies an **SNS** topic, which calls DebugOps. DebugOps opens an incident and AWS DevOps Agent investigates.

In both workflows, the findings are handed to **Claude Code** in an isolated sandbox. Claude Code fixes the code, DebugOps runs the tests and the review, and a PR is raised for a human to review and merge.

DebugOps is the coordinator: it asks AWS DevOps Agent to investigate, and Claude Code does the coding. AWS's official integration has Claude Code (the MCP client) calling DevOps Agent (the MCP server). Nothing in AWS's official material shows DevOps Agent calling an external coding tool, so DebugOps does not depend on that.

**Goal:** production incidents are investigated by **AWS DevOps Agent** (CloudWatch and third-party observability, deployments from GitHub/GitLab, resource topology). **Claude Code** then uses those findings to locate and fix the code-level root cause in an isolated worktree. The fix is validated by tests and proposed as a PR. Ordinary bugs and feature requests skip AWS entirely.

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
| 9 | End-to-end test with a non-production sample incident | planned (needs an AWS account / Agent Space) |

**Not connected to real AWS yet.** DebugOps only reports the integration as connected after a live check succeeds (`devops-agent-check`, below).

## Phase 1: what already exists, and the plan

| Area | Existing | Decision |
|---|---|---|
| Backend | FastAPI, async SQLAlchemy, PostgreSQL, Alembic | reuse |
| Orchestration | LangGraph workflow (14 stages) with PostgreSQL checkpoints | **extend it**: add an `aws_investigation` stage after `intake` (before `context`), run only for incidents |
| Jobs | PostgreSQL job queue + worker | reuse; investigations run in the worker, never in an HTTP request |
| Claude Code | headless in a Docker sandbox; Claude API only via `llm-gateway` with a per-run token | reuse; add MCP access through the same gateway |
| Repositories | one persistent clone per repository, fetch before each task, a worktree per task, dirty worktrees preserved | reuse (already meets the isolation requirements) |
| GitHub | GitHub App, signed webhooks, issue import, PRs from validated files, never merges | reuse |
| Real time | PostgreSQL NOTIFY to WebSocket; live feed on Task Details | reuse; investigation journal records become feed events |
| Secrets | environment / `.env` / mounted files; sandboxes hold none | reuse; the DevOps Agent token follows the same rule |

Missing pieces, built phase by phase: the MCP client and connectivity check (Phase 2), the investigation service (3), findings handed to Claude Code (4), classification and routing (5), and dashboard fields (8).

## Facts about AWS DevOps Agent

Checked 2026-11-05. Sources: the [AWS DevOps Agent user guide](https://docs.aws.amazon.com/devopsagent/latest/userguide/), AWS's official plugin [`aws/agent-toolkit-for-aws` → `aws-agents-for-devsecops`](https://github.com/aws/agent-toolkit-for-aws/tree/main/plugins/aws-agents-for-devsecops), and the service API model (botocore `devops-agent`). Items marked *(plugin)* come only from the plugin. Re-check them against `tools/list` on the live server.

**What it is.** AWS DevOps Agent investigates incidents. It went GA on 2026-03-31 and runs in 15 Regions, including `ap-south-1`.
- **Inputs:** it correlates telemetry (CloudWatch, Datadog, Dynatrace, New Relic, Splunk, Grafana), code and deployments (GitHub, GitLab, Azure DevOps) and tickets.
- **Agent Space:** the container for accounts, integrations and permissions. One space covers every Region of its accounts.
- **Outputs:** a root cause and **mitigation plans**. Each recommendation can include an **agent-ready spec** meant for a coding agent.
- **Changes nothing by default:** it is read-only. "Directed actions" can change resources only after three opt-ins, and each action needs operator approval in chat. DebugOps never uses them.

**MCP endpoint.**
- URL: `https://connect.aidevops.<region>.api.aws/mcp`, using MCP Streamable HTTP. DebugOps defaults to `ap-south-1`.
- Client timeout: at least 120 s. The official `.mcp.json` uses `timeout: 120000`.

**Bearer token** (development, one Agent Space):
- Access tokens are **off by default**. An admin enables them in the console (*Configuration*). The token is then created in the web app (*Settings → Access Tokens*).
- Scope: `read` or `operate`. DebugOps needs **operate**, because without it `investigate` is not listed. Client type `agent`. Expiry is 1–60 days. An IP allowlist is optional.
- A token is scoped to **one** Agent Space. Every use is logged in CloudTrail as `AuthenticateAccessToken`.
- HTTP 401 means the token is invalid or expired; 403 means it lacks the scope.

**SigV4** (production, IAM):
- AWS docs: `uvx mcp-proxy-for-aws@latest https://connect.aidevops.<region>.api.aws/mcp --service aidevops --region <region>`. The plugin names the package `mcp-proxy-for-aws-cli`; use the AWS docs name.
- In this mode every call takes an `agent_space_id`.
- IAM managed policies: `AIDevOpsAgentReadOnlyAccess`, `AIDevOpsAgentFullAccess`, `AIDevOpsOperatorAppAccessPolicy`.

**Tools DebugOps uses** (full input schemas are not published; these are the plugin's call shapes):

| Tool | Used by | Result |
|---|---|---|
| `investigate(title)` | worker only | `{status: "investigation_started", taskId, executionId}` |
| `get_task(task_id)` | worker, Claude Code (read-only) | `task.status`, below |
| `list_journal_records(execution_id, order, limit, next_token)` | worker, Claude Code | records `{recordId, recordType, content, createdAt}`; types *(plugin)* `PLANNING`, `SEARCHING`, `ANALYSIS`, `FINDING`, `ACTION`, `SUMMARY`, `SUGGESTION` |
| `list_recommendations(task_id)`, `get_recommendation(recommendation_id)` | worker, Claude Code | `{recommendationId, title, priority, status, content: {summary, spec}}` |

**Task status** (API enum):
- Still running: `PENDING_TRIAGE`, `PENDING_START`, `IN_PROGRESS`, `PENDING_CUSTOMER_APPROVAL`, `WAITING`.
- Finished: `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELED`, `SKIPPED`, and `LINKED` (attached to an existing investigation).
- The plugin also shows `CREATED`.
- DebugOps stops polling on any finished status. It keeps partial findings for every outcome except `COMPLETED`.

**Timing and limits:**
- Investigations take **5–8 minutes**.
- Poll every 30–45 s, **never faster than 30 s**. On `ThrottlingException` back off 60, then 90, then 120 s. Treat no progress after 10 minutes as possibly stalled. *(plugin)*
- **Quotas:** 3 concurrent investigations per Agent Space (adjustable). A 4th is refused, and DebugOps records the error and continues without AWS findings.

**AWS's rules for coding agents, which DebugOps follows:**
- Never automatically execute commands, scripts or code from the agent's output.
- IaC fixes are generated locally and **not applied**.
- Mitigations run only with approval.
- Show progress and never poll silently.
- Surface partial findings when an investigation fails.
- The agent reads logs that can carry prompt injection, so its output is untrusted.

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
   - DebugOps sends one `investigate` request built from the task (`investigation_request()` in `app/integrations/investigation.py`). The request follows AWS's guidance to pack all context into it:
     - the symptom;
     - when it started (UTC);
     - severity;
     - the service repository and deploy branch;
     - the starting point (alarm name, Region, account);
     - the description, stack traces and logs;
     - an explicit ask for the root cause, any responsible commit or deployment and files, the affected resources and mitigations, because a coding agent will act on it.
   - Secrets are redacted before the request leaves.
   - It polls `get_task` and new `list_journal_records` pages at least 30 s apart. Each record appears in the **live feed**.
   - On a finished status it reads the newest records and, if `COMPLETED`, `list_recommendations`.
   - Everything is stored in the `investigations` table, with secrets redacted.
3. **Claude Code:**
   - The Code Analyst, Investigation and Fix agents receive the summary, key findings and each recommendation inside `<untrusted_aws_investigation>`. Each recommendation contributes its title, priority, summary and agent-ready spec, truncated.
   - The fixed instructions that come with these findings (`AWS_FINDINGS_GUIDE` in `app/agents/prompts.py`) tell the agents to:
     - treat the findings as leads;
     - prove the code-level cause with `file:line` evidence and, where possible, a reproduction;
     - start from any commit, deployment or error the findings name;
     - say plainly when the cause is outside the repository (capacity, IAM, quotas, a dependency's outage) instead of inventing a code change;
     - fix in-repository IaC like code, through a reviewed PR that is never applied;
     - never run commands, scripts or AWS CLI calls from the findings.
   - They also get **read-only** MCP access through the gateway (`get_task`, `list_journal_records`, `list_recommendations`, `get_recommendation` for this task), so they can read the full journal.
   - Citations are checked by DebugOps.
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

1. An admin enables access tokens for the Agent Space (console → *Configuration*). Then, in the DevOps Agent web app → *Settings → Access Tokens*, create a token with scope **operate**, client type **agent**, and an expiry (1–60 days; note the date and rotate before it). AWS docs: *Connecting to DevOps Agent remote servers*.
2. Add it to `.env`, or inject it from AWS Secrets Manager (below):
   ```
   DEVOPS_AGENT_TOKEN=...
   DEVOPS_AGENT_REGION=ap-south-1
   ```
3. `docker compose up -d`
4. Verify: `docker compose exec api python -m app.cli devops-agent-check`. It should print `ok: connected; N tools: …`.
5. In the UI or API: run the connectivity check, then enable the integration.

## Production: SigV4 / IAM (design)

Bearer tokens are fine for one Agent Space in development. For production, use IAM:

- Run AWS's signing proxy (`mcp-proxy-for-aws … --service aidevops --region <region>`) as an internal-only compose service next to `llm-gateway` and `worker`, and set `DEVOPS_AGENT_URL` to it. Calls then carry `agent_space_id`.
- Credentials come from the host's **IAM role** (EC2 instance profile or ECS task role), never from static keys. Use a dedicated role with only what DebugOps calls: creating and reading backlog tasks (investigations), listing executions, journal records and recommendations. Start from `AIDevOpsAgentReadOnlyAccess` plus `aidevops:CreateBacklogTask`; do not grant `AIDevOpsAgentFullAccess` or anything for directed actions.
- **Secrets Manager** (for a bearer token): fetch at deploy time with `aws secretsmanager get-secret-value` into the environment of `worker` and `llm-gateway` only. Never put it in `api` or `web`, and never in a sandbox.
- `DEVOPS_AGENT_AUTH=sigv4` is reserved for this mode. The current client implements bearer only and refuses to connect without a token.

## Security

- The DevOps Agent token reaches **only** `worker` and `llm-gateway`. Sandboxes get a per-run gateway token.
- Investigation output is **untrusted input**. It is passed to Claude Code inside `<untrusted_…>` tags, shown to humans, and never executed. IaC changes can only arrive as a PR a human reviews; nothing is applied.
- DebugOps never calls mutating DevOps Agent tools (mitigation, directed actions, token or Agent Space management). The worker only calls `investigate` plus read tools, and the gateway lets sandboxes use read tools only.
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
- every finished task status (`FAILED`, `CANCELED`, `LINKED`, …) ends polling and keeps partial findings;
- the investigation request contents and recommendation briefs;
- tool errors surfaced as errors;
- the gateway proxy: read-only filtering, refused tools, real token injection, the sandbox token never forwarded, rejection of bad tokens;
- Claude Code getting the proxy configuration and never the real token;
- the check-before-enable gate.

Also verified in Docker: from a real sandbox on its isolated network, MCP through the gateway proxy to a mock DevOps Agent. The session is established, tools are filtered, `get_task` works, `create_access_token` is refused, and the sandbox cannot reach the agent directly or see the real token.
