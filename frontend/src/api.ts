export type Role = 'admin' | 'developer' | 'reviewer'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface User {
  id: number
  email: string
  name: string
  role: Role
  is_active: boolean
  last_login_at: string | null
  created_at: string
}

export interface Activity {
  id: number
  ts: string
  level: string
  type: string
  source: string
  message: string
}

export interface WorkerInfo {
  id: string
  started_at: string
  last_seen_at: string
  online: boolean
}

export interface DashboardSummary {
  tasks: { total: number; active: number; awaiting_review: number; succeeded: number; failed: number; needs_human: number; success_rate: number | null }
  errors_24h: number
  agent_status: { running: number; waiting: number; completed: number; failed: number }
  active_runs: { id: number; bug_title: string; status: string; current_node: string | null; attempt: number }[]
  open_bugs_by_severity: Record<string, number>
  open_bugs_by_source: Record<string, number>
  latest_tests: { passed: number; failed: number; errors: number; skipped: number }
  recent_scans: { id: number; repository: string; status: string; trigger: string; created_at: string; findings: number | null }[]
  jobs: Record<JobStatus, number>
  workers: WorkerInfo[]
  recent_activity: Activity[]
  users_by_role: Record<Role, number>
}

export interface AuditEntry {
  id: number
  ts: string
  actor_user_id: number | null
  actor_type: string
  action: string
  target_type: string | null
  target_id: string | null
  data: Record<string, unknown>
  ip: string | null
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

function csrfToken(): string {
  const c = document.cookie.split('; ').find((x) => x.startsWith('debugops_csrf='))
  return c ? decodeURIComponent(c.slice('debugops_csrf='.length)) : ''
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method: init.method ?? 'GET',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  })
  if (res.status === 204) return undefined as T
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const detail = data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join('; ') : 'Request failed'
    throw new ApiError(res.status, msg)
  }
  return data as T
}

export function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return new Date(iso).toLocaleDateString()
}

export interface GitHubStatus {
  configured: boolean
  problem: string | null
  app_id: string | null
  install_url: string | null
  api_url: string
  last_webhook_at: string | null
  installations: number
}

export interface Installation {
  id: number
  installation_id: number
  account_login: string
  account_type: string
  repository_selection: string
  permissions: Record<string, string>
  suspended: boolean
  deleted: boolean
}

export interface Repository {
  id: number
  full_name: string
  default_branch: string
  private: boolean
  accessible: boolean
  enabled: boolean
  installation_account: string
  installation_active: boolean
  last_access_check_at: string | null
  last_access_ok: boolean | null
  settings: RepoSettings
}

export interface AccessTest {
  ok: boolean
  token: { repository_ids: number[]; permissions: Record<string, string> }
  checks: { name: string; ok: boolean | null; detail: string }[]
}

export type RepoContents =
  | { type: 'dir'; path: string; ref: string; entries: { name: string; path: string; type: string; size: number }[] }
  | { type: string; path: string; ref: string; size: number; content: string | null; binary: boolean; truncated: boolean }

export type Severity = 'info' | 'low' | 'medium' | 'high' | 'critical'

export interface RepoSettings {
  run_semgrep: boolean
  run_gitleaks: boolean
  run_osv: boolean
  run_tests: boolean
  install_command: string | null
  test_command: string | null
  allow_install_scripts: boolean
  step_timeout_seconds: number
  scan_on_push: boolean
  scan_interval_hours: number
  ingest_labels: string[]
  auto_create_issues: boolean
  auto_issue_min_severity: Severity
  require_pr_approval: boolean
  comment_on_issue: boolean
  production_environment: string
  health_check_url: string | null
  health_check_minutes: number
  health_check_interval_seconds: number
  health_failure_threshold: number
  investigate_regressions: boolean
}

export interface Scan {
  id: number
  repository_id: number
  repository: string
  trigger: string
  status: string
  commit_sha: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  steps: Record<string, { status: string; detail?: string; duration_ms?: number }>
  summary: { findings?: number; new?: number; seen_again?: number; reopened?: number; failed_steps?: string[];
    by_severity?: Record<string, number>; tests?: Record<string, number> }
  error: string | null
}

export interface TestRunInfo {
  id: number
  command: string
  exit_code: number | null
  timed_out: boolean
  result_format: string
  passed: number
  failed: number
  errors: number
  skipped: number
  duration_ms: number
  log_tail: string
  results: { test_id: string; status: string; duration_ms: number; message: string }[]
}

export interface Bug {
  id: number
  repository_id: number
  repository: string
  source: string
  title: string
  severity: Severity
  status: string
  affected_files: { path: string; line_start: number | null; line_end: number | null }[]
  github_issue_number: number | null
  github_issue_url: string | null
  occurrences: number
  last_seen_at: string
  created_at: string
  duplicate_of: number | null
  stale: boolean
}

export interface BugDetail extends Bug {
  description: string
  data: Record<string, unknown>
  evidence: { id: number; kind: string; content: string; file_path: string | null; line_start: number | null;
    line_end: number | null; commit_sha: string | null; scan_run_id: number | null; created_at: string }[]
  similar: { id: number; title: string; status: string; source: string; similarity: number }[]
}

export const SOURCE_LABEL: Record<string, string> = {
  github_issue: 'GitHub issue', manual: 'Reported in DebugOps', auto_health: 'Production health', auto_test: 'Failing test', auto_semgrep: 'Code analysis',
  auto_gitleaks: 'Secret scan', auto_osv: 'Dependency',
}
export const SEVERITY_COLOR: Record<string, string> = {
  critical: 'bg-bad text-white', high: 'text-bad border border-bad', medium: 'text-warn border border-warn',
  low: 'text-muted border border-line', info: 'text-muted border border-line',
}

export interface RunSummary {
  id: number
  task_id: string
  task_title: string
  task_description: string
  base_sha: string | null
  branch_name: string | null
  files_changed: string[]
  test_summary: { passed?: number; failed?: number; error?: number; skipped?: number; total?: number; verdict?: string; regression_tests?: number }
  execution_seconds: number
  pr_url: string | null
  bug_id: number
  bug_title: string
  repository_id: number
  repository: string
  status: string
  current_node: string | null
  attempt: number
  validation_verdict: string | null
  started_by: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  tokens_in: number
  tokens_out: number
  cost_usd: number
  error: string | null
}

export interface Issue { severity: string; file: string; line: number | null; description: string }

export interface Delivery {
  review_state: string | null
  reviews: { user: string; state: string; at: string }[]
  merge_commit_sha: string | null
  post_merge_ci_status: string | null
  post_merge_checks: { name: string; status: string; conclusion: string | null }[]
  deployments: { id: number; environment: string; state: string; url: string; at: string }[]
  health: { status?: string; url?: string; until?: string; bug_id?: number; consecutive_failures?: number;
    recent?: { at: string; ok: boolean; status: number | null; ms: number }[] }
}

export interface RunDetail extends Omit<RunSummary, 'branch_name' | 'base_sha'> {
  nodes: Record<string, { status: string; detail?: string; attempt?: number; started_at?: string; finished_at?: string }>
  node_order: string[]
  branch_name: string | null
  base_sha: string | null
  steps: { id: number; node: string; agent: string; status: string; attempt: number; output: Record<string, unknown>;
    tokens_in: number; tokens_out: number; cost_usd: number; started_at: string; finished_at: string | null; error: string | null }[]
  attempts: { id: number; attempt_no: number; status: string; fix_paths: string[]; test_paths: string[];
    validation: { verdict?: string; reason?: string; evidence?: Record<string, unknown> };
    security: { verdict?: string; introduced_findings?: { title: string; severity: string; file: string; line: number }[]; agent?: { issues: Issue[]; summary?: string } };
    review: { verdict?: string; summary?: string; issues?: Issue[] }; created_at: string }[]
  approvals: { id: number; kind: string; user_id: number; decision: string; reason: string; created_at: string }[]
  pull_request: ({ id: number; number: number; url: string; state: string; head_branch: string; ci_status: string;
    checks: { name: string; status: string; conclusion: string | null; url?: string }[]; merged_at: string | null } & Delivery) | null
  report: { root_cause: string; evidence: { file: string; line: number; explanation?: string }[];
    affected: { path: string; kind: string; ranges: [number, number][]; deleted?: boolean }[]; suggested_fix: string;
    test_summary: { verdict?: string; reason?: string; evidence?: Record<string, any> }; security_summary: Record<string, any>;
    review: { verdict?: string; summary?: string; issues?: Issue[] }; markdown: string } | null
}

export const RUN_STATUS_COLOR: Record<string, string> = {
  queued: 'text-warn', running: 'text-accent', waiting_approval: 'text-warn', awaiting_merge: 'text-accent',
  succeeded: 'text-ok', needs_human: 'text-warn', failed: 'text-bad', rejected: 'text-muted', cancelled: 'text-muted', closed: 'text-muted',
}

export function duration(seconds: number): string {
  if (!seconds) return '–'
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  return m < 60 ? `${m}m ${seconds % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}
