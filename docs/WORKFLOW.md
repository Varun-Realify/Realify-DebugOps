# The Debugging Workflow

**Detect bug or developer reports bug → AI investigates → finds root cause → fixes code → runs tests → creates PR → developer reviews, approves and merges → CI/CD deploys → production health is watched.**

Bugs arrive from automated scans, GitHub issues, a developer's report in DebugOps (**Bugs → Report bug**, with stack traces and log files), CloudWatch alarms (via SNS) and failed post-deploy health checks. One engine handles all of them. Start a run from a bug's page with **Investigate with AI**, or tick "Start AI investigation now" when reporting; alarms can start runs automatically.

Each task is classified first. **Production incidents** are investigated by AWS DevOps Agent before any code is read; ordinary **bugs** and **feature requests** go straight to Claude Code. Both paths then follow the same steps: investigate → fix → test → review → PR.

```
intake ─▶ aws_investigation ─▶ context ─▶ analysis ─▶ investigation ─▶ root-cause gate ─▶ fix ─▶ testing ─▶ validation ─▶ security ─▶ review ─▶ report ─▶ human approval ─▶ pull request ─▶ (human merge, CI)
(classify)  (incidents only)                                               │               ▲                  │            │          │
                                                                           └─needs human   └── retry gate ◀───┴────────────┴──────────┘ (max 3 attempts)
```

| Stage | Who | What happens | Trust rule |
|---|---|---|---|
| intake | platform rules, then Claude API; Bug Hunter for scan findings | Classifies the task as **incident**, **bug** or **feature** (alarm/health-check sources and labels such as `incident`, `prod`, `outage`, `sev1`, `p0` decide first; the AI decides only when no rule applies, preferring *bug*). Scan findings are triaged; likely false positives stop here | Triage only stops a run on high confidence |
| aws_investigation | AWS DevOps Agent (MCP), driven by the worker | Incidents only, and only when the integration is enabled: starts an investigation, streams its journal live, stores findings and recommendations | Findings are untrusted input for later agents, never executed. A failed or timed-out investigation does not stop the run |
| context | platform | Fetches the latest default branch into the persistent clone, creates the task worktree on branch `debugops/dbg-NNN-<title>`, copies it into a fresh sandbox, installs test dependencies | Token never enters the sandbox; the shared clone is never mounted into it |
| analysis | Code Analyst (Claude Code, read-only) | Maps relevant files, symbols, dependencies (for incidents, starting from the AWS findings) | Output is input to Investigation, which must verify it |
| investigation | Investigation (Claude Code, read-only + test commands) | Up to 3 root-cause hypotheses with file:line evidence | Every citation is checked against the source; invented ones are discarded |
| root-cause gate | platform | Needs one hypothesis with verified evidence and confidence ≥ 0.6 | Otherwise the run ends **needs human** with a report |
| fix | Fix agent (Claude Code, edit) | Minimal source change (for a feature: the requested behaviour) | Edits to tests are reverted automatically |
| testing | Testing agent (Claude Code, edit) | Writes a regression test | Edits to non-test code are reverted; deleting tests is undone |
| validation | **platform** | Runs the suite 3 ways: original code; original code + new test; fix + new test | **Verified** only if the new test fails before the fix, passes after it, and no previously passing test breaks. Suites without per-test results are **unverifiable**, and the run never auto-opens a PR |
| security | Semgrep + Gitleaks on changed files, plus the Security agent | New high/critical findings block the change | Agent claims count only if concrete (high/critical, in a changed file) |
| review | Review agent (Claude API, separate context) | approve / changes requested / reject | Sees the diff and platform evidence, **never the fixer's reasoning** |
| report | platform | Root cause, evidence, affected files and lines, diff, test results, security, review | Says honestly whether the fix is validated |
| approval *(optional)* | **human** (Reviewer or Admin, not the run's starter) | Extra gate before the PR is opened | **Off by default**: enable per repository with `require_pr_approval` |
| pull request | platform (GitHub API) | Creates commit and branch `debugops/dbg-NNN-<title>` from the base commit, opens the PR with the report, comments on the issue | Never pushes to an existing branch; no merge capability exists |
| review + merge | **developer** in GitHub | Reviews, approves and merges the PR. Reviews are recorded | Bug becomes **resolved** only after a human merges a validated fix |
| CI / build | GitHub Actions (your CI) | Checks on the PR and on the merge commit are tracked | Failures are shown on the run |
| deploy | your CD, under its approval policy | DebugOps records GitHub Deployments for the merge commit | DebugOps never deploys |
| production health | platform (optional per repository) | Polls a health URL after a successful production deploy for a set window | Repeated failures open a **critical regression bug**, optionally investigated automatically |

Failed validation, a security block, or "changes requested" sends feedback to the Fix agent for another attempt (default maximum 3). Every stage is checkpointed in PostgreSQL, so a worker restart resumes the run where it stopped.

**Cost and limits:** per run, the gateway caps Claude requests and tokens. The run page shows tokens and estimated cost per agent step.

## Repository management rules

| # | Rule | How DebugOps implements it |
|---|---|---|
| 1 | One persistent local clone per repository | A bare clone at `/var/lib/debugops/repos/<repo id>.git` on the worker's `repo-cache` volume (`app/repocache.py`) |
| 2 | Never clone again if it exists | Created only if missing; afterwards only fetched. If the repository is renamed or transferred, the remote URL is updated instead of re-cloning |
| 3 | Fetch and pull the latest base branch before every task | `git fetch --prune` runs before every scan and every debugging run (and again when a run resumes); the base commit is resolved from the freshly fetched branch |
| 4 | Never modify the shared base branch | The clone has no local base branch at all: base branches exist only as `refs/remotes/origin/*`, updated only by fetching from GitHub. PRs target the base branch; DebugOps never pushes to it and cannot merge |
| 5 | Isolated branch or worktree per task | Each run gets its own worktree on its own branch `debugops/dbg-NNN-<title>`; each scan gets a detached worktree `scan-N`. Inside the sandbox the agents work on that branch |
| 6 | Change code only in the isolated workspace | Agents edit only inside the sandbox copy of the task worktree. The validated files become a new commit on `debugops/dbg-NNN-<title>`. Nothing writes to the shared clone's history |
| 7 | Run relevant tests before a PR | Validation runs the suite on the original code, the original code plus the new regression test, and the fix; plus security scans and an independent review. A PR is only possible after a **verified** result |
| 8 | Never merge or deploy without human approval | A Reviewer/Admin approves PR creation; merging happens only by a human in GitHub; there is no merge or deploy code path |
| 9 | Preserve uncommitted changes; never overwrite others' work | A worktree with uncommitted changes is never removed. If a `debugops/` branch already exists on GitHub and its tip is not this run's own commit, PR creation stops and asks for a human instead of overwriting it |

## Audit record (stored in PostgreSQL for every run)

| Field | Where | Example |
|---|---|---|
| Task ID | `workflow_runs.id` shown as `DBG-NNN` | DBG-001 |
| Bug description | `task_title`, `task_description` (snapshot at start) | Login API returns 500 |
| Repository | `repository_full_name` (snapshot) | realify/api |
| Base commit | `base_sha` | 3f2c9e1… |
| Branch | `branch_name` | debugops/dbg-001-login-api-returns-500 |
| Agent activity | `agent_steps` (agent, status, output, tokens, cost, timing) and `activity_events` | Bug Hunter → Code Analyst → Investigation → Fix → Testing → Security → Review |
| Files changed | `files_changed` (+ full diff and file contents in `fix_attempts`) | app/auth.py, tests/test_auth.py |
| Tests | `test_summary` (+ every run in `test_runs` / `test_results`) | 120 passed, 0 failed |
| Execution time | `execution_seconds` (active time; waits for humans excluded) | 4m 12s |
| PR URL | `pr_url` (+ reviews, CI, deployments, health in `pull_requests`) | https://github.com/realify/api/pull/77 |
| Final status | `status` | awaiting_merge |

Export: **Runs → Export audit trail (CSV)** or `GET /api/v1/runs/export.csv`.
