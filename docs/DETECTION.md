# Bug Detection

DebugOps receives bugs from GitHub issues, developer reports, automated scans, CloudWatch alarms and post-deploy health checks. All of them produce **bug records** in one shared list, which feeds the debugging workflow ([WORKFLOW.md](WORKFLOW.md)).

## A. Manual: GitHub issues

- **Live:** when an issue is opened, edited, labeled, closed or reopened in an *enabled* repository, the webhook creates or updates the matching bug.
- **Backfill:** Repositories, then **Import issues**, ingests currently open issues.
- **Filter:** only issues carrying one of the repository's `ingest_labels` are ingested (default `bug`, `debugops`; empty means all). An issue that is already tracked keeps syncing even if its labels later change.
- **Severity** comes from labels: `critical`, `high`, `severity:high`, `p0`, `p1`, and so on. The default is medium.
- Pull requests and issues DebugOps itself opened are never ingested.
- Closing an issue on GitHub sets the bug to `closed`, which is deliberately not `resolved`: a closed issue is not proof of a fix.

## A2. Manual: reported in DebugOps

Developers can report a bug directly in **Bugs → Report bug**, with a description, steps to reproduce, a stack trace, suspected files and up to 5 log files (text, 1 MB each).
- Secrets in all of it are redacted before storage.
- Similar existing bugs are shown right away.
- Optionally a GitHub issue is opened and an AI investigation starts immediately.

## A3. Automatic: CloudWatch alarms (production incidents)

A CloudWatch alarm entering `ALARM` publishes to an SNS topic that DebugOps subscribes to (`POST /api/v1/aws/sns`). The message is signature-verified, matched to a repository by its `alarm_patterns`, and recorded as a bug with source `aws_alarm` and label `incident` (one bug per alarm, account and region; a repeat firing increments its occurrence count). If the repository has `auto_investigate_alarms` on, a debugging run starts and AWS DevOps Agent investigates first. Setup: [AWS_DEVOPS_AGENT.md](AWS_DEVOPS_AGENT.md).

Failed post-deploy health checks (source `auto_health`) are handled the same way: they open a critical regression bug, classified as an incident.

## B. Automatic: scans

A scan runs against the default branch head. It can be triggered manually (**Scan now**), on a schedule (`scan_interval_hours`), or on push (`scan_on_push`). Each repository has at most one queued or running scan at a time.

| Step | What happens |
|---|---|
| fetch | The worker mints a token for **this repository only** (`contents:read`), fetches the latest code into the repository's persistent clone, and creates a detached worktree `scan-N` at the default branch head. The worktree's content is copied into the sandbox; the token never enters it. Unsafe paths are rejected, device files are dropped, and size limits are enforced. The worktree is removed after the scan. |
| sandbox | A throwaway container from `debugops-sandbox`. See isolation below. |
| semgrep | Semgrep with the bundled `p/default` ruleset, offline, metrics off. |
| gitleaks | Secret scan of the working tree. Each secret is SHA-256 hashed for duplicate detection and masked immediately. **Plaintext secrets are never stored or shown.** |
| osv | OSV-Scanner over lockfiles and manifests. It queries api.osv.dev through the egress proxy. |
| tests | Runs the repository's tests (see below) and stores per-test results. Each failing test becomes a bug. |
| record | Deduplicates and stores bugs and evidence. |

If one tool fails, the scan is marked **partial** and the failure is shown. The other tools' results are still recorded, and a failure is never silently dropped.

### Tests

The test command is auto-detected unless `test_command` is set:

- **Python** (any of pyproject/setup.py/requirements*.txt plus `test_*.py` files): creates a venv, `pip install pytest` plus requirements, then `pytest --junitxml`.
- **Node** (a `package.json` `test` script): `npm ci`/`npm install` with `--ignore-scripts` (unless `allow_install_scripts`), then vitest (JUnit), jest (JSON) or `npm test` (exit code only).
- **Custom:** set `install_command` / `test_command` in repository settings. Write JUnit XML to `/workspace/reports/junit.xml` to get per-test results; otherwise only the exit code is used.

### Duplicate handling

Every finding has a stable **fingerprint**, and a repeat finding updates the existing bug instead of creating a new one:

| Source | Fingerprint |
|---|---|
| Semgrep | rule + file + normalized code (not the line number, so code moving down a file stays one bug) |
| Gitleaks | rule + file + hash of the secret |
| OSV | ecosystem + package + advisory + manifest |
| Test | test id + normalized error (numbers and quoted values stripped) |
| GitHub issue | issue number |

- A bug that was `resolved` or `closed` and is detected again is **reopened**.
- Bugs not detected in the latest scan are marked "not in latest scan". They are **not** auto-resolved, because absence from one scan is not validated proof of a fix.
- The bug page lists **possible duplicates** (similar titles, via PostgreSQL `pg_trgm`) that a developer can mark as duplicates.
- Issues DebugOps opens carry a hidden `<!-- debugops:fp=… -->` marker, which is checked before opening another.

### Automatic GitHub issues

These are off by default. Enable them per repository with `auto_create_issues` and `auto_issue_min_severity` (default `high`). They require the App's **Issues: write** permission.

- Security findings (Semgrep, Gitleaks, OSV) are **never** filed as public issues on public repositories.
- Developers can also open an issue for a single bug from its page.

## Sandbox isolation

- Non-root (uid 10001), read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`.
- Memory, CPU and process limits (`SANDBOX_MEMORY`, `SANDBOX_CPUS`, `SANDBOX_PIDS`).
- Every command is wrapped in `timeout -s KILL` (`step_timeout_seconds`), and the container itself exits after `SANDBOX_SCAN_TIMEOUT_SECONDS`.
- **Network:** the sandbox sits on an internal-only Docker network. Its only way out is `egress-proxy`, which allows just the domains in `egress-proxy/allowlist.txt` (package registries and OSV). GitHub, cloud metadata endpoints and everything else are unreachable.
- **No credentials:** no GitHub token, no API keys, no DebugOps configuration.
- The worker reaches Docker only through `docker-proxy`, which exposes the container, exec and volume endpoints only.
- Containers and volumes are removed after each scan. Orphans from a crashed worker are cleaned up when the worker starts.

To allow another domain (for example a private package registry), add it to `egress-proxy/allowlist.txt`, then run `docker compose up -d --build egress-proxy`.

## Known limitations

- The sandbox receives the head commit's files without git history, so secrets that exist only in **history** are not scanned. The persistent clone does hold full history, so history scanning can be added there.
- Semgrep rules are fixed at sandbox image build time. Rebuild the image (`docker compose build sandbox-image`) to update them.
- Test auto-detection covers Python and Node. Other stacks need a custom `test_command`.
- Detection-time triage is deterministic (severity mapping and fingerprints). The Bug Hunter agent triages a scan finding when a debugging run starts, and stops the run on a confident false positive.
