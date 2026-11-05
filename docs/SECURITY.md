# Security Model

DebugOps runs untrusted code (repository tests, dependency installs) and AI agents that read untrusted text (issues, code). It is built on two assumptions: an agent can be prompt-injected, and repository code can be malicious. Neither may lead to credential theft, cross-repository access, unreviewed code reaching a protected branch, or deployment.

## Trust boundaries

```
 Browser ──HTTPS──▶ web/api ──▶ postgres
                      │
 GitHub ──webhook(HMAC)──▶ api            worker ──(scoped tokens)──▶ GitHub API
                                            │  ▲
                              docker-proxy ◀┘  │ diffs, results (data only)
                                            ▼  │
                              sandbox (untrusted) ──▶ egress-proxy ──▶ allowlisted registries / OSV
                                  │
                                  └──(per-run token)──▶ llm-gateway ──(real key)──▶ Claude API
```

| Component | Holds | Never holds |
|---|---|---|
| sandbox | repository snapshot, a per-run gateway token | GitHub tokens, Anthropic key, DB credentials, `.env` |
| llm-gateway | Anthropic key, DB access for token checks | GitHub key, session secret |
| worker | GitHub App key, Anthropic key, DB | — |
| api | session secret, GitHub key (for API calls), DB | — |

## Controls

| Threat | Control | Verified by |
|---|---|---|
| Forged webhooks / replay | HMAC-SHA256 with constant-time compare; delivery-id dedupe | `test_github.py` (GitHub's published test vector) |
| Cross-repository access | installation tokens minted with `repository_ids=[one repo]` and minimal permissions per call; repository must be admin-enabled | `test_github.py`, `test_detection.py` |
| Credential exposure to agents/code | sandbox gets no secrets; Claude Code reaches the API via the gateway with a per-run token, revoked when the sandbox is released | `test_agents.py`; live e2e: real Claude Code CLI to gateway to upstream; real key absent from the sandbox |
| Secrets leaking into storage | Gitleaks hits hashed then masked; generic redaction on all evidence, logs, issue bodies | `test_parsers.py`, real-sandbox test asserts no plaintext anywhere |
| Malicious repository code | non-root, read-only rootfs, `cap_drop ALL`, no-new-privileges, memory/CPU/PID limits, per-command `timeout -s KILL`, container hard deadline, internal-only network | `test_sandbox.py` (caps=0, OOM, fork bomb, timeout, no DNS, no secrets in env) |
| Data exfiltration from sandbox | only egress is the allowlisting proxy (registries + OSV); GitHub and everything else blocked | live check: pypi 200, example.com / github.com / direct access blocked |
| Poisoning the persistent clone | the clone and its worktrees stay on the worker's volume and are never mounted into a sandbox; every git command runs with hooks disabled (`core.hooksPath=/dev/null`), no system/global config, no prompts; the token goes per command in `http.extraheader` and is never stored in git config or error messages | `test_repocache.py` |
| Unsafe archives | repack rejects `..`/absolute paths, drops devices/hardlinks, enforces size/file limits | `test_sandbox.py` |
| Path traversal / injection via API | repository paths and refs validated before any GitHub call; no `shell=True` in the control plane; branch names generated (`debugops/run-N`) | `test_github.py` (10 traversal cases) |
| Prompt injection | untrusted text delimited; agents hold no credentials; every side effect (push, PR, issue) is deterministic code behind validation and human gates; Fix agent can't edit tests, Testing agent can't edit code (enforced by diff); citations verified; reviewer never sees the fixer's rationale | `test_workflow.py` |
| "Fixed" without evidence | platform-run fail-before / pass-after regression check plus no newly failing tests; suites without per-test results are `unverifiable` and never auto-PR'd | `test_workflow.py` (useless test, unverifiable, wrong fix) |
| Unreviewed merge / deploy | no merge code path; App never granted Administration; PR creation needs Reviewer/Admin approval (not the run's starter); merging stays in GitHub | `test_workflow.py` (separation of duties, no merge calls) |
| Session attacks | server-side sessions (hashed), HttpOnly/SameSite/Secure cookies, session-bound CSRF token, Origin check, idle and absolute expiry, revocation on password/role change | `test_auth.py`, `e2e_smoke.py` |
| Brute force | per-account lockout (5 failures/15 min), per-IP throttle (20/min), identical failure responses | `test_auth.py`, `test_security.py` |
| Missing authorization | every route except login, webhook and health requires a session; the sweep test enumerates routes from the app itself | `test_security.py` (all routes) |
| WebSocket hijacking | session cookie and same-origin check; session re-validated every 60 s | `e2e_smoke.py` |
| Tampering with audit history | append-only trigger on `audit_logs` | `test_auth.py` |
| Supply chain (sandbox image) | scanner binaries verified against pinned sha256; Semgrep and Claude Code versions pinned | build log |

## Known residual risks

1. **Docker access is root-equivalent.** `docker-proxy` limits the worker to the container, exec and volume endpoints, but cannot restrict container parameters. A compromised *worker* could start a privileged container. Run DebugOps on a dedicated host. Optional hardening: the gVisor runtime (`runsc`) for sandboxes.
2. **Allowlisted registries can host malicious packages.** Dependency installs run inside the sandbox, with npm install scripts disabled by default, but a malicious package can still run code inside the sandbox during tests. It cannot leave the allowlist or reach credentials.
3. **A sandboxed agent can spend its run budget.** Bounded per run by the gateway. Also set a workspace spend limit at Anthropic.
4. **Scan coverage.** Secrets that exist only in git history are not scanned; the sandbox receives the head commit's files only.
5. **Single api replica assumptions.** The login throttle and the WebSocket broker are per process. If you scale the api, put rate limiting in the reverse proxy.

## Reporting

Report suspected vulnerabilities to the DebugOps owners privately, not in a public issue. Rotate secrets as described in OPERATIONS.md if compromise is suspected.
