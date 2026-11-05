# Realify DebugOps — Architecture & Implementation Plan (Phase 0)

Status: **Phases 0–8 implemented.**

Implementation notes and deviations from this plan:
- The AI Bug Hunter triage moved from Phase 3 to Phase 4, because it needs the agent layer.
- **Claude Code reaches the API through an `llm-gateway` service**, not an egress proxy that injects the key (risk R3). Each run gets its own token, the gateway enforces per-run request and token budgets, and the token is revoked when the run's sandbox is released.
- Security, Review and Bug Hunter agents call the Claude API directly from the worker, with structured outputs. They need no repository access.
- Pull requests are created with the GitHub Git Data API from the stored validated files, rather than by `git push` from a clone. No push credentials exist anywhere near a sandbox.
- Live updates come from PostgreSQL triggers that publish through `NOTIFY`, so every service publishes events without extra code.
- **Repository management:** one persistent bare clone per repository on the worker (`repo-cache` volume), fetched before every task, with one git worktree per task (see WORKFLOW.md). Replaced the earlier per-task tarball download.
- The sandbox receives the head commit's files only, so secrets that exist only in git history are not scanned (see SECURITY.md).
- Default model: `claude-opus-5-5` (configurable with `AGENT_MODEL`).  ·  Date: 2026-11-05

---

## 0. Workspace Inspection

- Project directory was **empty**: no existing code, dependencies, or git history. Nothing to reuse or overwrite.
- Tooling available on the build machine (dev only — production will not depend on it):
  Python 3.15, Node 26, Docker 29, PostgreSQL 16 client, uv, Semgrep 1.183, Gitleaks 8.30, OSV-Scanner 2.7, Claude Code 2.1.
- Containers will pin **Python 3.12** and **Node 22 LTS** for library compatibility (LangGraph, asyncpg, and others lag behind Python 3.15).

---

## 1. Architecture Proposal

### 1.1 Stack (as requested, with three deliberate decisions)

| Concern | Choice |
|---|---|
| Frontend | React 19 + TypeScript + Vite, TanStack Query, React Flow (`@xyflow/react`), Tailwind CSS |
| Backend API | Python 3.12 + FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2 |
| Orchestration | LangGraph with `langgraph-checkpoint-postgres` (durable, resumable runs) |
| AI coding | Claude Code via the **Claude Agent SDK** (headless), run inside a sandbox container |
| Database | PostgreSQL 16 |
| Real-time | FastAPI WebSockets, fed by Postgres `LISTEN/NOTIFY` |
| GitHub | GitHub App (JWT → per-repo installation tokens), REST + webhooks |
| Scanners | Semgrep, Gitleaks, OSV-Scanner (all baked into the sandbox image) |
| Packaging | Docker + Docker Compose |

**Decision A: No Redis or Celery. PostgreSQL is the job queue and the event bus.**
Jobs use `SELECT … FOR UPDATE SKIP LOCKED` and live events use `LISTEN/NOTIFY`. This means one fewer stateful service for Realify to run, back up, and secure. The expected load is tens of concurrent debugging runs, not thousands per second, which Postgres handles easily.
Ceiling: if throughput ever exceeds roughly 100 jobs/s, swap in Redis Streams behind the same `queue.py` interface.

**Decision B: Agents never hold GitHub write credentials.**
Agent containers receive a short-lived, **read-only, single-repository** token, and only for the clone. Pushing branches, opening PRs, and creating issues are done by the control plane, and only after the deterministic validation gates and the human gates pass. A compromised or prompt-injected agent therefore cannot push code or touch other repositories.

**Decision C: "Fixed" is decided by the platform, not by an agent.**
The platform itself re-runs the tests and requires fail-to-pass evidence: the regression test **fails on the base commit and passes on the fix commit**, and the full suite introduces no new failures. Agent claims are recorded but are never the source of truth.

### 1.2 Deployable units

```
┌──────────────┐   HTTPS   ┌──────────────────────────────────────────────┐
│  Browser     │──────────▶│  web (nginx: static React build + /api proxy)│
└──────────────┘    WS     └───────────────┬──────────────────────────────┘
                                           │
GitHub ──webhooks──▶ ┌─────────────────────▼───────────┐
        ◀──REST───── │  api (FastAPI)                   │  auth, RBAC, REST, WS,
                     │                                  │  webhook intake, audit
                     └───────┬──────────────────▲───────┘
                             │ jobs (SKIP LOCKED)│ NOTIFY events
                     ┌───────▼──────────────────┴───────┐
                     │  postgres                         │  app data, queue,
                     │                                   │  LangGraph checkpoints
                     └───────▲──────────────────────────┘
                             │
                     ┌───────┴──────────────────────────┐
                     │  worker (LangGraph orchestrator)  │  runs the workflow graph,
                     │  holds GitHub App key + Anthropic │  calls GitHub, gates
                     └───────┬──────────────────────────┘
                             │ via docker-socket-proxy (create/start/stop/rm only)
                     ┌───────▼──────────────────────────┐
                     │  sandbox containers (ephemeral)   │  1 per task: clone,
                     │  no secrets, CPU/mem/pids/time cap│  tests, scanners,
                     │  egress allowlist only            │  Claude Code
                     └──────────────────────────────────┘
```

Compose services: `web`, `api`, `worker`, `postgres`, `docker-proxy`, `egress-proxy` (Squid with an allowlist), plus a `sandbox` image that is built but not run as a long-lived service.

---

## 2. System Design

### 2.1 Two intake paths, one engine

```
A) GitHub Issue (opened/labeled "debugops")      B) Scan (manual / scheduled / push)
   └─ webhook → verify HMAC → dedupe delivery       └─ Bug Hunter: tests + Semgrep +
      → Bug(source=github_issue)                       Gitleaks + OSV → triage → dedupe
                                                       → Bug(source=auto_*) → optional
                                                         GitHub Issue (with evidence)
                      └──────────────┬──────────────┘
                                     ▼
                        WorkflowRun (LangGraph graph)
```

### 2.2 Workflow graph (LangGraph nodes)

| # | Node | Kind | Output / gate |
|---|---|---|---|
| 1 | `intake` | deterministic | Normalized `BugReport` |
| 2 | `context_collection` | deterministic | Clone at pinned SHA, gather issue, comments, and linked logs |
| 3 | `repo_analysis` | **Code Analyst** | Relevant files, symbols, dependency map |
| 4 | `investigation` | **Investigation** | Hypotheses ranked by confidence, each with evidence refs |
| 5 | `root_cause` | gate | Requires ≥1 hypothesis with confidence ≥ threshold and reproducible evidence; otherwise routes to `needs_human` |
| 6 | `fix` | **Fix** (Claude Code) | Patch inside a worktree on branch `debugops/<run-id>` |
| 7 | `regression_test` | **Testing** | New or updated test(s) |
| 8 | `validation` | **deterministic** | Run test on base (must FAIL), on fix (must PASS), full suite (no new failures). On failure, loop back to `fix` (max 3 attempts), then `failed` |
| 9 | `security` | scanners + **Security** | Semgrep/Gitleaks/OSV on the diff; the agent triages. Blocks on new high/critical findings |
| 10 | `review` | **Review** | Independent verdict built from the raw diff and raw logs. `changes_requested` loops to `fix`; `reject` routes to `needs_human` |
| 11 | `await_pr_approval` | **human gate** (LangGraph `interrupt`) | Developer/Reviewer approves opening the PR (can be disabled per repository) |
| 12 | `open_pr` | deterministic | Control plane pushes the branch and opens the PR with the full report |
| 13 | `await_merge_ci` | polling + webhooks | Track check runs and review state. **Never merges.** |
| 14 | `done` | — | Status = `merged` / `closed` / `ci_failed` |

Deployment is **out of scope by design**: after merge, Realify's existing CI/CD and authorized deployment process take over. DebugOps only reports CI status.

### 2.3 Isolation per task

- Each run gets a fresh container and a fresh volume. The repo is cloned with a token scoped to `repositories=[that_repo]`, `contents:read`. Work happens on a new branch in that clone; the user's branches are never checked out for writing.
- Limits: `--cpus`, `--memory`, `--pids-limit`, wall-clock timeout, `--read-only` root filesystem with tmpfs, `--cap-drop=ALL`, `no-new-privileges`, non-root user, `--network` attached only to the egress proxy.
- Egress allowlist: `api.anthropic.com`, `github.com` (clone only), and package registries (PyPI, npm) for dependency install. Configurable.
- The patch leaves the sandbox as a `git diff` artifact. The worker applies and pushes it from its own side.

---

## 3. Agent Architecture

### 3.1 Shared state (LangGraph `TypedDict`, persisted by the checkpointer)

```python
class RunState(TypedDict):
    run_id: str; repo_id: int; bug_id: int; base_sha: str
    bug: BugReport
    analysis: CodeAnalysis | None
    hypotheses: list[Hypothesis]
    root_cause: RootCause | None
    fix_attempts: list[FixAttempt]          # patch, rationale, files touched
    tests: list[GeneratedTest]
    validation: list[ValidationResult]      # produced by the platform, not an agent
    security: SecurityReport | None
    review: ReviewVerdict | None
    approvals: list[Approval]
    pr: PullRequestRef | None
    status: RunStatus
```

### 3.2 Agents

Every agent returns a **Pydantic-validated** structured output. A schema failure triggers one retry and then fails the step; free-form text is never parsed.

| Agent | Runtime | Tools allowed | Output |
|---|---|---|---|
| Bug Hunter | Claude (API) + deterministic scanners | read files, scanner/test results | `list[Finding]` (with evidence refs, severity, fingerprint) |
| Investigation | Claude Code, read-only | Read, Grep, Glob, run tests (allowlisted commands) | `list[Hypothesis]` (claim, evidence[file:line, log excerpt], confidence) |
| Code Analyst | Claude Code, read-only | Read, Grep, Glob | `CodeAnalysis` (files, symbols, call graph excerpt) |
| Fix | Claude Code, write in worktree only | Read, Edit, Write, run tests | `FixAttempt` (rationale; the diff is captured by the platform) |
| Testing | Claude Code, write in `tests/` paths | Read, Edit, Write, run tests | `list[GeneratedTest]` |
| Security | Claude (API) on scanner output + diff | none (input only) | `SecurityReport` |
| Review | Claude (API), **separate context** | none (input only) | `ReviewVerdict` (approve / changes_requested / reject, issues with file:line) |

### 3.3 Not trusting each other

- Investigation evidence must cite `file:line`. The platform **checks that each cited line exists** at `base_sha` and drops hypotheses with fabricated references.
- The Review agent receives **raw artifacts only**: the diff, the platform's test logs, and the scanner output. It never sees the Fix agent's rationale before forming a verdict (anchoring defense).
- The Testing agent's test must fail on base. A test that passes on unfixed code proves nothing and is rejected.
- Fix and Testing agents are restricted by path: Fix may not edit test files that already exist (it cannot "fix" by weakening tests), and Testing may only write under the configured test directories. Both are enforced by diff inspection after the run, not by trusting the agent.
- Issue bodies, comments, and repository contents are **untrusted input** (prompt-injection surface). They are delimited in prompts, agents have no credentials, and every side-effect is performed by deterministic code behind gates.

### 3.4 Command execution

Agents never run arbitrary shell. Claude Code runs with `--allowedTools` limited to file tools plus `Bash(<allowlisted test/lint commands>)`. The allowlist is derived per repository (for example `pytest …`, `npm test`, `npx vitest run …`) and stored in repository settings that an Admin can edit. Everything runs inside the sandbox regardless.

---

## 4. Database Schema (PostgreSQL)

All tables have `id bigserial PK`, `created_at`, and `updated_at`. FK = foreign key.

```
users                 email UNIQUE, password_hash (argon2id), role ENUM(admin,developer,reviewer),
                      is_active, last_login_at
sessions              id (random 256-bit, stored hashed), user_id FK, expires_at, ip, user_agent, revoked_at
audit_logs            actor_user_id FK NULL, actor_type ENUM(user,system,agent), action, target_type,
                      target_id, metadata JSONB, ip            -- append-only (no UPDATE/DELETE grant)

github_installations  installation_id UNIQUE, account_login, account_type, permissions JSONB, suspended_at
repositories          installation_id FK, github_repo_id UNIQUE, full_name, default_branch, private,
                      enabled BOOL, settings JSONB             -- test cmds, allowlist, auto-issue, auto-investigate,
                                                               -- require_pr_approval, scan schedule, limits
webhook_deliveries    delivery_id UNIQUE, event, action, repo_id FK NULL, status, error, received_at

scan_runs             repo_id FK, trigger ENUM(manual,schedule,push), commit_sha, status, started_at,
                      finished_at, summary JSONB
bugs                  repo_id FK, source ENUM(github_issue,auto_test,auto_semgrep,auto_gitleaks,auto_osv,auto_ai),
                      title, description, severity ENUM(info,low,medium,high,critical),
                      status ENUM(new,triaged,investigating,fix_proposed,pr_open,resolved,wont_fix,duplicate),
                      fingerprint, duplicate_of FK NULL, github_issue_number NULL, scan_run_id FK NULL,
                      affected_files JSONB                     -- UNIQUE(repo_id, fingerprint) WHERE duplicate_of IS NULL
evidence              bug_id FK NULL, run_id FK NULL, kind ENUM(log,stack_trace,scanner,test_output,diff,file_ref),
                      content TEXT (secrets redacted), file_path, line_start, line_end, sha256

workflow_runs         bug_id FK, repo_id FK, base_sha, branch_name, status, current_node, attempt,
                      thread_id (LangGraph), started_by FK NULL, started_at, finished_at, error
agent_steps           run_id FK, node, agent, status ENUM(waiting,running,completed,failed,skipped),
                      input_ref, output JSONB, tokens_in, tokens_out, cost_usd, started_at, finished_at, error
activity_events       run_id FK NULL, step_id FK NULL, level, type ENUM(action,file_read,command,test,error,info),
                      message, data JSONB, ts                  -- feeds the live log, NOTIFY on insert
test_runs             run_id FK NULL, scan_run_id FK NULL, commit_sha, phase ENUM(baseline,base,fix,full,scan),
                      command, exit_code, passed, failed, skipped, errors, duration_ms, raw_log_ref
test_results          test_run_id FK, test_id, status, duration_ms, message, is_regression BOOL
security_findings     run_id FK NULL, scan_run_id FK NULL, tool, rule_id, severity, file_path, line,
                      fingerprint, introduced_by_fix BOOL, message, redacted_snippet
fix_attempts          run_id FK, attempt_no, patch TEXT, files_changed JSONB, rationale, validation_status
reviews               run_id FK, reviewer_type ENUM(agent,human), user_id FK NULL, verdict, comments JSONB
approvals             run_id FK, kind ENUM(open_pr,write_permission,rerun), user_id FK, decision, reason
pull_requests         run_id FK, repo_id FK, number, url, head_branch, state, merged_at, ci_status, checks JSONB
reports               run_id FK UNIQUE, root_cause, evidence JSONB, affected JSONB, suggested_fix,
                      test_summary JSONB, security_summary JSONB, markdown TEXT

jobs                  queue, payload JSONB, status, priority, run_at, attempts, max_attempts, locked_by,
                      locked_at, last_error                    -- worker claims with SKIP LOCKED
(langgraph checkpoint tables — created and owned by langgraph-checkpoint-postgres)
```

Duplicate detection:
1. A deterministic **fingerprint**: for scanners, `sha256(tool + rule_id + path + normalized code snippet)`; for tests, `sha256(test_id + normalized error signature)`.
2. Fuzzy title/description match with `pg_trgm` similarity against open bugs in the same repository.
3. A hidden `<!-- debugops:fp=<hash> -->` marker in created GitHub issues. Issues are searched for the marker before creating a new one.

---

## 5. API Endpoint Plan (`/api/v1`)

OpenAPI is auto-generated at `/api/docs` (Admin only in production).

**Auth**
- `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` · `POST /auth/change-password`

**Users** (Admin)
- `GET/POST /users` · `PATCH /users/{id}` (role, active) · `DELETE /users/{id}` (soft delete)

**GitHub**
- `POST /github/webhook` (public; HMAC verified)
- `GET /github/installations` · `POST /github/installations/sync` (Admin)
- `GET /repositories` · `PATCH /repositories/{id}` (enable, settings; Admin)
- `POST /repositories/{id}/test-access` (Admin)

**Scans**
- `POST /repositories/{id}/scans` (Developer+) · `GET /scans` · `GET /scans/{id}`

**Bugs**
- `GET /bugs?repo=&status=&severity=&source=` · `GET /bugs/{id}` · `PATCH /bugs/{id}` (status, severity)
- `POST /bugs/{id}/mark-duplicate`
- `POST /bugs/{id}/investigate` (Developer+; starts a WorkflowRun)

**Runs**
- `GET /runs` · `GET /runs/{id}` (state, steps, graph)
- `GET /runs/{id}/events?after=` · `GET /runs/{id}/tests` · `GET /runs/{id}/security` · `GET /runs/{id}/diff`
- `GET /runs/{id}/report` (JSON + Markdown)
- `POST /runs/{id}/approve` · `POST /runs/{id}/reject` (Reviewer/Admin; human gate)
- `POST /runs/{id}/cancel` · `POST /runs/{id}/retry` (Developer+)

**Pull requests**
- `GET /pull-requests` · `POST /pull-requests/{id}/refresh-ci`

**Other**
- `GET /audit-logs` (Admin)
- `GET /dashboard/summary` (agent status counts, recent bugs, test totals)
- `GET /health/live` · `GET /health/ready`

**WebSocket:** `WS /ws?topics=runs,run:{id},bugs` authenticates with the session cookie and checks Origin. It sends a backlog `after=<event_id>` on reconnect, so no events are lost.

**RBAC matrix**

| Capability | Admin | Developer | Reviewer |
|---|---|---|---|
| View everything | ✓ | ✓ | ✓ |
| Trigger scans / investigations, retry/cancel | ✓ | ✓ | — |
| Approve/reject PR creation (human gate) | ✓ | — | ✓ |
| Manage repositories, settings, users, audit log | ✓ | — | — |

A run's starter cannot approve that same run (separation of duties).

---

## 6. Frontend Folder Structure

```
frontend/
├── Dockerfile · nginx.conf · package.json · vite.config.ts · tsconfig.json · index.html
└── src/
    ├── main.tsx · App.tsx · routes.tsx
    ├── api/            client.ts (fetch + CSRF), types.ts (generated from OpenAPI), queries.ts
    ├── auth/           AuthProvider.tsx, RequireRole.tsx, LoginPage.tsx
    ├── realtime/       useEventStream.ts (WS with reconnect + backlog)
    ├── components/     Layout, Sidebar, StatusBadge, SeverityBadge, DataTable, CodeRef, DiffView, LogViewer
    ├── pages/
    │   ├── Dashboard.tsx        agent status tiles, recent activity, bug & test summaries
    │   ├── Bugs.tsx · BugDetail.tsx
    │   ├── Runs.tsx · RunDetail.tsx   tabs: Workflow (React Flow) | Logs | Tests | Security | Diff | Report
    │   ├── Repositories.tsx · RepositorySettings.tsx
    │   ├── PullRequests.tsx
    │   └── admin/Users.tsx · admin/AuditLog.tsx
    └── features/workflow/  WorkflowGraph.tsx, nodeTypes.tsx, layout.ts
```

---

## 7. Backend Folder Structure

```
backend/
├── Dockerfile · pyproject.toml (uv) · alembic.ini
├── alembic/versions/
├── app/
│   ├── main.py              FastAPI app, routers, middleware (security headers, request id)
│   ├── config.py            pydantic-settings, all from env
│   ├── db.py                async engine/session
│   ├── models.py            SQLAlchemy models (split into a package once it grows)
│   ├── schemas/             Pydantic API schemas
│   ├── api/                 auth.py, users.py, github.py, repositories.py, scans.py, bugs.py,
│   │                        runs.py, pull_requests.py, audit.py, ws.py, health.py
│   ├── auth.py              passwords, sessions, CSRF, RBAC deps (require_role)
│   ├── audit.py
│   ├── events.py            publish (insert + NOTIFY) / subscribe (LISTEN) for WS
│   ├── queue.py             enqueue / claim (SKIP LOCKED) / complete / retry with backoff
│   ├── github/              app_auth.py (JWT, scoped install tokens), client.py, webhooks.py (HMAC),
│   │                        issues.py, prs.py, checks.py
│   ├── sandbox/             runner.py (docker via proxy, limits, timeouts), commands.py (allowlist),
│   │                        paths.py (traversal-safe path resolution)
│   ├── detection/           tests.py (runner + JUnit/JSON parsing), semgrep.py, gitleaks.py, osv.py,
│   │                        fingerprint.py, dedupe.py, redact.py
│   ├── agents/              base.py (structured-output call + retry), claude_code.py (Agent SDK wrapper),
│   │                        bug_hunter.py, investigation.py, code_analyst.py, fix.py, testing.py,
│   │                        security.py, review.py, prompts/
│   ├── workflow/            state.py, graph.py (LangGraph), nodes/ (one per node), gates.py
│   ├── reports.py           Markdown/JSON final report builder
│   └── worker.py            entrypoint: claim jobs, drive graphs, poll CI
└── tests/                   unit/, integration/ (real Postgres via compose), fixtures/sample_repos/
sandbox/
└── Dockerfile               non-root; git, python, node, semgrep, gitleaks, osv-scanner, claude-code
docker-compose.yml · docker-compose.override.yml (dev) · .env.example
docs/  ARCHITECTURE.md · INSTALL.md · GITHUB_APP.md · API.md · OPERATIONS.md (backup, recovery, troubleshooting)
```

---

## 8. Security Architecture

| Threat | Control |
|---|---|
| Forged webhooks | HMAC-SHA256 over the raw body, compared with `hmac.compare_digest`; reject when missing; dedupe on `X-GitHub-Delivery` (replay) |
| Cross-repository access | Installation tokens minted with `repositories=[id]` and minimum permissions per operation; repository must be `enabled`; every query scoped by `repo_id` |
| Credential theft | GitHub App private key and Anthropic key exist only in `worker`/`api` env (or Docker secrets files). Never in the frontend, sandboxes, logs, or DB |
| Production secrets reaching agents | Sandboxes get no env secrets except an Anthropic key (see Risk R3). Production config is never mounted. Gitleaks output is redacted before storage or display |
| Command injection | No `shell=True` anywhere; `subprocess`/Docker exec with argv lists; agent shell restricted to an allowlist of parsed commands; branch names generated by us (`debugops/<uuid>`) |
| Path traversal | All repository paths resolved with `Path.resolve()` and checked `is_relative_to(workspace)`; symlinks rejected when they escape the workspace |
| Sandbox escape / DoS | Non-root, cap-drop ALL, read-only rootfs, seccomp default, pids/mem/CPU limits, timeouts, per-repository concurrency cap, global run cap. Worker reaches Docker only through `docker-socket-proxy` (container create/start/stop/remove only) |
| Prompt injection via issues or code | Untrusted content is delimited; agents hold no credentials; side-effects are performed only by deterministic code behind validation and human gates; Review agent works from raw artifacts |
| Session attacks | Server-side sessions; cookies `HttpOnly; Secure; SameSite=Lax`; CSRF double-submit token on mutating requests; rotation on login; idle and absolute timeouts; login rate limiting with lockout |
| Privilege misuse | RBAC dependency on every route; separation of duties on approvals; audit log for logins, settings changes, approvals, PR/issue creation, run starts |
| Disclosure of vulnerabilities | For **public** repositories, security findings are never filed as public issues; they stay in the dashboard (optionally as GitHub private security advisories later) |
| Unsafe merges or deploys | The App is never granted `administration`; DebugOps has no merge code path; deployment is outside the system |

**GitHub App permissions, staged:**
- Phase 2 (read-only): `metadata:read`, `contents:read`, `issues:read`, `pull_requests:read`, `checks:read`. Events: `issues`, `issue_comment`, `push`, `pull_request`, `check_run`, `installation*`.
- Phase 3/6 (minimum writes, enabled when the workflow is ready): `issues:write` (auto-created issues and comments), `contents:write` (push `debugops/*` branches only), `pull_requests:write`.
- Recommend adding a branch ruleset in each repository that blocks the App from pushing to anything but `debugops/*`. This is defense in depth on GitHub's side.

---

## 9. Phase-by-Phase Implementation Plan

Each phase ends with tests run, a report, and a stop for approval.

| Phase | Deliverables | Exit tests |
|---|---|---|
| **1 Foundation** | Compose (postgres, api, worker, web), config via env, Alembic baseline (users, sessions, audit_logs, jobs, activity_events), argon2 login, sessions, CSRF, RBAC, admin bootstrap CLI, user management, layout and dashboard shell with real `/dashboard/summary` | pytest: auth, sessions, RBAC matrix, CSRF; `docker compose up` health checks; frontend typecheck and build |
| **2 GitHub** | App JWT and scoped installation tokens, installation sync, repository list and enable, webhook endpoint (HMAC, dedupe, persistence), repository content fetch, `test-access`, `GITHUB_APP.md` | Unit: HMAC vectors, token scoping; integration against a real test App and private repository (needs Realify credentials) |
| **3 Detection** | Sandbox image and runner (limits, egress proxy), test runner with JUnit parsing, Semgrep/Gitleaks/OSV adapters, redaction, fingerprints and dedupe, issue ingestion, scan runs, auto-issue creation (behind setting), Bug Hunter triage | Fixture repos with known bugs, secrets, and vulnerable dependencies produce expected findings; duplicates suppressed; redaction verified; sandbox limit tests (timeout, fork bomb, network block) |
| **4 Agents** | LangGraph graph and checkpointer, state, structured outputs, Investigation, Code Analyst, root-cause gate, evidence citation verification | Graph tests with a stubbed LLM (deterministic); one live smoke run on a fixture repository; resume after worker kill |
| **5 Fix & Test** | Claude Code wrapper (Agent SDK), worktree and branch, Fix and Testing agents, path restrictions, deterministic fail-to-pass validation, retry loop, Security and Review agents | Fixture bug fixed end-to-end; a test that passes on base is rejected; edits to existing tests by Fix are rejected; review loop exercised |
| **6 PRs** | Human approval gate (interrupt/resume), branch push, PR with report, CI checks via webhooks and polling, issue comments, write permissions enabled | Live test on a sandbox repository: PR opened, never merged, CI status reflected; approval RBAC and separation of duties |
| **7 Live dashboard** | WS with LISTEN/NOTIFY and backlog, live agent status, logs, bugs, tests, React Flow graph, reports, diff viewer | Playwright e2e: run progresses live; reconnect loses no events |
| **8 Production readiness** | Security review (authz, injection, traversal, sandbox), failure recovery, concurrency and load test, resource limits, backups, INSTALL/OPERATIONS/API docs, full e2e | Documented results for each; restore-from-backup drill |

---

## 10. Technical Risks

| ID | Risk | Mitigation |
|---|---|---|
| R1 | Docker socket access is root-equivalent on the host | `docker-socket-proxy` restricting the API surface; recommend a dedicated VM. Optional gVisor (`runsc`) runtime for sandboxes |
| R2 | Unknown project languages and test setups in Realify's repositories | Per-repository settings for install and test commands; Python and JS/TS supported first; JUnit XML as the universal result format |
| R3 | Claude Code inside the sandbox needs an Anthropic credential | Use a dedicated, spend-limited workspace key, and route through the egress proxy which **injects the key** so the sandbox never sees it (fallback: a separate low-limit key) |
| R4 | LLM cost and latency per run | Token and cost accounting per step; per-run and per-day budget caps; cheaper model for triage, stronger model for fix and review (model IDs configurable via env) |
| R5 | Flaky tests corrupt fail-to-pass evidence | Baseline run on base; rerun failures N times; flaky tests are marked and excluded from gating |
| R6 | Prompt injection through issue text or repository content | Section 3.3 controls; no credentials in agents; human gate before any GitHub write |
| R7 | Dependency install needs network and runs arbitrary package scripts | Runs inside the sandbox only, through the allowlisted egress proxy; never on the host or the worker |
| R8 | GitHub API rate limits on large organizations | Per-installation token cache, conditional requests (ETag), webhook-first design with polling as fallback |

---

## 11. Open Questions (needed before or during Phase 1–3)

1. **Languages and test frameworks** in Realify's target repositories? Python and JS/TS are assumed first.
2. **Hosting target**: a single Linux VM running Docker Compose is assumed. Kubernetes would change the sandbox runner (Jobs instead of the Docker API).
3. **Anthropic access**: is there an organization API key or workspace for DebugOps?
4. **Login**: local accounts are planned for Phase 1. Is GitHub/Google SSO required? It can be added later without schema changes.
