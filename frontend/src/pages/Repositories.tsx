import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, timeAgo, type AccessTest, type GitHubStatus, type Installation, type RepoContents, type RepoSettings, type Repository, type Scan, type User } from '../api'

export default function Repositories({ me }: { me: User }) {
  return (
    <Routes>
      <Route index element={<RepoList isAdmin={me.role === 'admin'} canRun={me.role !== 'reviewer'} />} />
      <Route path=":id/browse" element={<Browser />} />
    </Routes>
  )
}

function RepoList({ isAdmin, canRun }: { isAdmin: boolean; canRun: boolean }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<Repository | null>(null)
  const scanNow = useMutation({ mutationFn: (r: Repository) => api<Scan>(`/repositories/${r.id}/scans`, { method: 'POST' }),
    onSuccess: (sc) => navigate(`/scans/${sc.id}`) })
  const importIssues = useMutation({ mutationFn: (r: Repository) => api(`/repositories/${r.id}/import-issues`, { method: 'POST' }) })
  const status = useQuery({ queryKey: ['gh-status'], queryFn: () => api<GitHubStatus>('/github/status') })
  const installs = useQuery({ queryKey: ['gh-installs'], queryFn: () => api<Installation[]>('/github/installations') })
  const repos = useQuery({ queryKey: ['repos'], queryFn: () => api<Repository[]>('/repositories') })
  const [tests, setTests] = useState<Record<number, AccessTest | string>>({})
  const refresh = () => ['gh-status', 'gh-installs', 'repos'].forEach((k) => qc.invalidateQueries({ queryKey: [k] }))

  const sync = useMutation({ mutationFn: () => api<Record<string, number>>('/github/sync', { method: 'POST' }), onSuccess: refresh })
  const toggle = useMutation({
    mutationFn: (r: Repository) => api<Repository>(`/repositories/${r.id}`, { method: 'PATCH', body: { enabled: !r.enabled } }),
    onSuccess: refresh,
  })
  const test = useMutation({
    mutationFn: (r: Repository) => api<AccessTest>(`/repositories/${r.id}/test-access`, { method: 'POST' }),
    onSuccess: (res, r) => { setTests((t) => ({ ...t, [r.id]: res })); refresh() },
    onError: (e, r) => setTests((t) => ({ ...t, [r.id]: (e as Error).message })),
  })

  const s = status.data
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Repositories</h1>
        {isAdmin && s?.configured && (
          <div className="flex gap-2">
            {s.install_url && <a className="btn-ghost" href={s.install_url} target="_blank" rel="noreferrer">Install on GitHub</a>}
            <button className="btn-primary" onClick={() => sync.mutate()} disabled={sync.isPending}>
              {sync.isPending ? 'Syncing…' : 'Sync from GitHub'}
            </button>
          </div>
        )}
      </header>

      {s && !s.configured && (
        <div className="card border-warn p-4 text-sm">
          <div className="font-medium text-warn">GitHub App not configured</div>
          <p className="mt-1 text-muted">
            Set <code className="font-mono">GITHUB_APP_ID</code>, the private key and <code className="font-mono">GITHUB_WEBHOOK_SECRET</code> in
            <code className="font-mono"> .env</code>, then restart. Step-by-step guide: <code className="font-mono">docs/GITHUB_APP.md</code>.
          </p>
        </div>
      )}
      {s?.problem && <div className="card border-bad p-4 text-sm text-bad">{s.problem}</div>}
      {sync.error && <p role="alert" className="text-sm text-bad">Sync failed: {sync.error.message}</p>}
      {sync.data && <p role="status" className="text-sm text-ok">Synced {sync.data.installations} installation(s), {sync.data.repositories} repositories.</p>}
      {toggle.error && <p role="alert" className="text-sm text-bad">{toggle.error.message}</p>}
      {scanNow.error && <p role="alert" className="text-sm text-bad">{scanNow.error.message}</p>}
      {importIssues.error && <p role="alert" className="text-sm text-bad">{importIssues.error.message}</p>}
      {importIssues.isSuccess && <p role="status" className="text-sm text-ok">Issue import queued. New bug reports will appear on the Bugs page.</p>}

      {s?.configured && (
        <section className="grid gap-3 sm:grid-cols-3">
          <div className="card p-4"><div className="label">App ID</div><div className="font-mono">{s.app_id}</div></div>
          <div className="card p-4"><div className="label">Installations</div><div className="font-mono">{s.installations}</div></div>
          <div className="card p-4"><div className="label">Last webhook</div><div>{timeAgo(s.last_webhook_at)}</div></div>
        </section>
      )}

      {installs.data && installs.data.some((i) => i.suspended || i.deleted) && (
        <ul className="space-y-1 text-sm">
          {installs.data.filter((i) => i.suspended || i.deleted).map((i) => (
            <li key={i.id} className="text-warn">Installation on <b>{i.account_login}</b> is {i.deleted ? 'uninstalled' : 'suspended'}; its repositories are unavailable.</li>
          ))}
        </ul>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[720px]">
          <thead>
            <tr><th className="th">Repository</th><th className="th">Access</th><th className="th">DebugOps</th><th className="th">Last check</th><th className="th" /></tr>
          </thead>
          <tbody>
            {repos.data?.length === 0 && (
              <tr><td className="td py-8 text-center text-muted" colSpan={5}>
                No repositories yet. {isAdmin ? 'Install the GitHub App on your organization, then Sync.' : 'Ask an admin to connect GitHub.'}
              </td></tr>
            )}
            {repos.data?.map((r) => {
              const t = tests[r.id]
              const usable = r.accessible && r.installation_active
              return (
                <tr key={r.id} className="align-top">
                  <td className="td">
                    <div className="font-medium">{r.full_name}</div>
                    <div className="text-xs text-muted">{r.private ? 'private' : 'public'} · {r.default_branch}</div>
                    {t && <AccessResult result={t} />}
                  </td>
                  <td className="td">{usable ? <span className="text-ok">Granted</span> : <span className="text-bad">Removed</span>}</td>
                  <td className="td">
                    {isAdmin ? (
                      <label className="inline-flex cursor-pointer items-center gap-2">
                        <input type="checkbox" checked={r.enabled} disabled={!usable || toggle.isPending} onChange={() => toggle.mutate(r)} />
                        {r.enabled ? 'Enabled' : 'Disabled'}
                      </label>
                    ) : r.enabled ? 'Enabled' : <span className="text-muted">Disabled</span>}
                  </td>
                  <td className="td text-sm">
                    {r.last_access_ok === null ? <span className="text-muted">never</span> :
                      <span className={r.last_access_ok ? 'text-ok' : 'text-bad'}>{r.last_access_ok ? 'OK' : 'Failed'} · {timeAgo(r.last_access_check_at)}</span>}
                  </td>
                  <td className="td whitespace-nowrap text-right">
                    {isAdmin && (
                      <button className="btn-ghost mr-2" disabled={!usable || (test.isPending && test.variables?.id === r.id)} onClick={() => test.mutate(r)}>
                        Test access
                      </button>
                    )}
                    {r.enabled && usable && <Link className="btn-ghost mr-2" to={`${r.id}/browse`}>Browse</Link>}
                    {r.enabled && usable && canRun && <>
                      <button className="btn-ghost mr-2" onClick={() => importIssues.mutate(r)} disabled={importIssues.isPending}>Import issues</button>
                      <button className="btn-primary" onClick={() => scanNow.mutate(r)} disabled={scanNow.isPending}>Scan now</button>
                    </>}
                    {isAdmin && <button className="btn-ghost ml-2" onClick={() => setEditing(r)}>Settings</button>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {repos.error && <p className="p-4 text-sm text-bad">{repos.error.message}</p>}
      </div>
      {editing && <SettingsDialog repo={editing} onClose={() => { setEditing(null); refresh() }} />}
    </div>
  )
}

function SettingsDialog({ repo, onClose }: { repo: Repository; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  const [s, setS] = useState<RepoSettings>(repo.settings)
  const [labels, setLabels] = useState(repo.settings.ingest_labels.join(', '))
  const save = useMutation({
    mutationFn: () => api(`/repositories/${repo.id}`, { method: 'PATCH', body: { settings: { ...s,
      install_command: s.install_command || null, test_command: s.test_command || null,
      ingest_labels: labels.split(',').map((x) => x.trim()).filter(Boolean) } } }),
    onSuccess: onClose,
  })
  const check = (k: keyof RepoSettings, label: string, hint?: string) => (
    <label className="flex items-start gap-2 text-sm">
      <input type="checkbox" className="mt-1" checked={s[k] as boolean} onChange={(e) => setS({ ...s, [k]: e.target.checked })} />
      <span>{label}{hint && <span className="block text-xs text-muted">{hint}</span>}</span>
    </label>
  )
  return (
    <dialog ref={(el) => { ref.current = el; if (el && !el.open) el.showModal() }} onClose={onClose}
      className="card m-auto w-full max-w-2xl p-6 text-fg backdrop:bg-black/50">
      <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-5">
        <h2 className="font-semibold">Settings · {repo.full_name}</h2>
        <fieldset className="grid gap-2 sm:grid-cols-2">
          <legend className="label">Detectors</legend>
          {check('run_tests', 'Run tests')}{check('run_semgrep', 'Semgrep code analysis')}
          {check('run_gitleaks', 'Gitleaks secret scan')}{check('run_osv', 'OSV dependency scan')}
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">Tests (run inside the sandbox; leave empty to auto-detect Python/Node)</legend>
          <input className="input font-mono" placeholder="Install command, e.g. pip install -r requirements.txt" value={s.install_command ?? ''}
            onChange={(e) => setS({ ...s, install_command: e.target.value })} aria-label="Install command" />
          <input className="input font-mono" placeholder="Test command; write JUnit XML to /workspace/reports/junit.xml for per-test results" value={s.test_command ?? ''}
            onChange={(e) => setS({ ...s, test_command: e.target.value })} aria-label="Test command" />
          {check('allow_install_scripts', 'Allow npm install scripts', 'Off by default: lifecycle scripts run arbitrary code during install.')}
          <label className="block text-sm">Step timeout (seconds)
            <input type="number" min={30} max={3600} className="input mt-1 w-32" value={s.step_timeout_seconds}
              onChange={(e) => setS({ ...s, step_timeout_seconds: Number(e.target.value) })} /></label>
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">Triggers</legend>
          {check('scan_on_push', 'Scan when the default branch receives a push')}
          <label className="block text-sm">Scheduled scan every (hours, 0 = off)
            <input type="number" min={0} max={720} className="input mt-1 w-32" value={s.scan_interval_hours}
              onChange={(e) => setS({ ...s, scan_interval_hours: Number(e.target.value) })} /></label>
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">GitHub issues</legend>
          <label className="block text-sm">Ingest issues with these labels (comma separated; empty = all issues)
            <input className="input mt-1" value={labels} onChange={(e) => setLabels(e.target.value)} /></label>
          {check('auto_create_issues', 'Automatically open GitHub issues for new detected bugs',
            'Requires the App to have Issues: write. Security findings are never filed on public repositories.')}
          <label className="block text-sm">Minimum severity for automatic issues
            <select className="input mt-1 w-auto" value={s.auto_issue_min_severity}
              onChange={(e) => setS({ ...s, auto_issue_min_severity: e.target.value as RepoSettings['auto_issue_min_severity'] })}>
              {['critical', 'high', 'medium', 'low', 'info'].map((x) => <option key={x}>{x}</option>)}
            </select></label>
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">Pull requests</legend>
          {check('require_pr_approval', 'Require approval in DebugOps before the AI opens a PR',
            'Off: the AI opens the PR after validation and developers review, approve and merge it in GitHub. Merging is always human.')}
          {check('comment_on_issue', 'Comment on the originating GitHub issue with the PR link')}
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">Production incidents (CloudWatch alarms via SNS)</legend>
          <label className="block text-sm">Alarm names belonging to this repository (glob patterns, one per line)
            <textarea className="input mt-1 min-h-16 font-mono text-xs" placeholder="realify-api-*" value={s.alarm_patterns.join('\n')}
              onChange={(e) => setS({ ...s, alarm_patterns: e.target.value.split('\n').map((x) => x.trim()).filter(Boolean) })} /></label>
          {check('auto_investigate_alarms', 'Start the incident workflow automatically when a matching alarm fires',
            'AWS DevOps Agent investigates, then Claude Code fixes, tests and opens a PR for review.')}
        </fieldset>
        <fieldset className="space-y-3">
          <legend className="label">After merge: deployment and production health</legend>
          <p className="text-xs text-muted">DebugOps never deploys. It records GitHub Deployments from your CD, and can watch a health URL after a production deploy.</p>
          <label className="block text-sm">Production environment name (as reported by your CD)
            <input className="input mt-1" value={s.production_environment} onChange={(e) => setS({ ...s, production_environment: e.target.value })} /></label>
          <label className="block text-sm">Health check URL (empty = off)
            <input className="input mt-1 font-mono" placeholder="https://api.realify.ai/health" value={s.health_check_url ?? ''}
              onChange={(e) => setS({ ...s, health_check_url: e.target.value || null })} /></label>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="block text-sm">Watch for (minutes)<input type="number" min={1} max={1440} className="input mt-1" value={s.health_check_minutes}
              onChange={(e) => setS({ ...s, health_check_minutes: Number(e.target.value) })} /></label>
            <label className="block text-sm">Every (seconds)<input type="number" min={15} max={600} className="input mt-1" value={s.health_check_interval_seconds}
              onChange={(e) => setS({ ...s, health_check_interval_seconds: Number(e.target.value) })} /></label>
            <label className="block text-sm">Failures before regression<input type="number" min={1} max={20} className="input mt-1" value={s.health_failure_threshold}
              onChange={(e) => setS({ ...s, health_failure_threshold: Number(e.target.value) })} /></label>
          </div>
          {check('investigate_regressions', 'Start an AI investigation automatically when a regression is detected')}
        </fieldset>
        {save.error && <p role="alert" className="text-sm text-bad">{save.error.message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={() => ref.current?.close()}>Cancel</button>
          <button className="btn-primary" disabled={save.isPending}>Save</button>
        </div>
      </form>
    </dialog>
  )
}

function AccessResult({ result }: { result: AccessTest | string }) {
  if (typeof result === 'string') return <p className="mt-2 text-sm text-bad">{result}</p>
  return (
    <div className="mt-2 rounded-md border border-line bg-bg p-3 text-xs">
      <div className="mb-1 text-muted">
        Token scoped to 1 repository · {Object.entries(result.token.permissions).map(([k, v]) => `${k}:${v}`).join(', ')}
      </div>
      <ul className="space-y-0.5">
        {result.checks.map((c) => (
          <li key={c.name} className="flex gap-2">
            <span className={c.ok === null ? 'text-muted' : c.ok ? 'text-ok' : 'text-bad'}>{c.ok === null ? '–' : c.ok ? '✓' : '✗'}</span>
            <span className="font-medium">{c.name}</span>
            <span className="text-muted">{c.detail}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Browser() {
  const { id } = useParams()
  const [params, setParams] = useSearchParams()
  const path = params.get('path') ?? ''
  const repos = useQuery({ queryKey: ['repos'], queryFn: () => api<Repository[]>('/repositories') })
  const repo = repos.data?.find((r) => String(r.id) === id)
  const q = useQuery({
    queryKey: ['contents', id, path],
    queryFn: () => api<RepoContents>(`/repositories/${id}/contents?path=${encodeURIComponent(path)}`),
  })
  const crumbs = path ? path.split('/') : []
  const go = (p: string) => setParams(p ? { path: p } : {})

  return (
    <div className="space-y-4">
      <div className="text-sm"><Link to="/repositories" className="text-muted hover:text-fg">← Repositories</Link></div>
      <h1 className="text-xl font-semibold tracking-tight">{repo?.full_name ?? 'Repository'}</h1>
      <nav aria-label="Path" className="flex flex-wrap items-center gap-1 font-mono text-sm">
        <button className="text-accent hover:underline" onClick={() => go('')}>{repo?.default_branch ?? 'root'}</button>
        {crumbs.map((c, i) => (
          <span key={i}>/ <button className="text-accent hover:underline" onClick={() => go(crumbs.slice(0, i + 1).join('/'))}>{c}</button></span>
        ))}
      </nav>
      {q.error && <p className="text-sm text-bad">{q.error.message}</p>}
      {q.isLoading && <p className="text-sm text-muted">Loading…</p>}
      {q.data && q.data.type === 'dir' && 'entries' in q.data && (
        <ul className="card divide-y divide-line">
          {q.data.entries.map((e) => (
            <li key={e.path}>
              <button className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm hover:bg-bg" onClick={() => go(e.path)}>
                <span className="w-8 text-xs text-muted">{e.type === 'dir' ? 'dir' : ''}</span>
                <span className={e.type === 'dir' ? 'font-medium' : ''}>{e.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {q.data && 'content' in q.data && (
        q.data.binary ? <p className="text-sm text-muted">Binary file ({q.data.size} bytes).</p> :
        q.data.truncated ? <p className="text-sm text-muted">File too large to display ({q.data.size} bytes).</p> :
        <pre className="card overflow-x-auto p-4 font-mono text-xs leading-relaxed">{q.data.content}</pre>
      )}
    </div>
  )
}
