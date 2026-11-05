import { useQuery } from '@tanstack/react-query'
import { Link, Route, Routes, useParams } from 'react-router-dom'
import { api, timeAgo, type Scan, type TestRunInfo } from '../api'

const STATUS_COLOR: Record<string, string> = {
  queued: 'text-warn', running: 'text-accent', succeeded: 'text-ok', partial: 'text-warn', failed: 'text-bad',
  ok: 'text-ok', skipped: 'text-muted',
}
const active = (s?: string) => s === 'queued' || s === 'running'

export default function Scans() {
  return (
    <Routes>
      <Route index element={<ScanList />} />
      <Route path=":id" element={<ScanView />} />
    </Routes>
  )
}

function duration(s: Scan) {
  if (!s.started_at) return ''
  const ms = new Date(s.finished_at ?? Date.now()).getTime() - new Date(s.started_at).getTime()
  return ms < 60000 ? `${Math.round(ms / 1000)}s` : `${Math.round(ms / 60000)}m`
}

function ScanList() {
  const q = useQuery({
    queryKey: ['scans'], queryFn: () => api<Scan[]>('/scans'),
    refetchInterval: (query) => (query.state.data?.some((s) => active(s.status)) ? 3000 : false),
  })
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Scans</h1>
      <p className="text-sm text-muted">Start a scan from the Repositories page. Scans run tests, Semgrep, Gitleaks and OSV-Scanner in an isolated sandbox.</p>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[720px]">
          <thead><tr><th className="th">Scan</th><th className="th">Status</th><th className="th">Findings</th><th className="th">Tests</th><th className="th">When</th></tr></thead>
          <tbody>
            {q.data?.length === 0 && <tr><td className="td py-8 text-center text-muted" colSpan={5}>No scans yet.</td></tr>}
            {q.data?.map((s) => (
              <tr key={s.id}>
                <td className="td"><Link to={String(s.id)} className="font-medium hover:text-accent">#{s.id} {s.repository}</Link>
                  <div className="text-xs text-muted">{s.trigger}{s.commit_sha && <> · <span className="font-mono">{s.commit_sha.slice(0, 12)}</span></>}</div></td>
                <td className={`td text-sm font-medium ${STATUS_COLOR[s.status] ?? ''}`}>{s.status}</td>
                <td className="td text-sm">{s.summary.findings ?? '–'}{s.summary.new ? <span className="text-muted"> ({s.summary.new} new)</span> : null}</td>
                <td className="td text-sm">{s.summary.tests ? `${s.summary.tests.passed} ✓ ${s.summary.tests.failed + s.summary.tests.errors} ✗` : '–'}</td>
                <td className="td text-sm text-muted">{timeAgo(s.created_at)} {duration(s) && `· ${duration(s)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {q.error && <p className="p-4 text-sm text-bad">{q.error.message}</p>}
      </div>
    </div>
  )
}

function ScanView() {
  const { id } = useParams()
  const scan = useQuery({
    queryKey: ['scan', id], queryFn: () => api<Scan>(`/scans/${id}`),
    refetchInterval: (query) => (active(query.state.data?.status) ? 2000 : false),
  })
  const tests = useQuery({ queryKey: ['scan-tests', id, scan.data?.status], queryFn: () => api<TestRunInfo[]>(`/scans/${id}/tests`), enabled: !!scan.data })
  const s = scan.data
  if (scan.error) return <p className="text-bad">{scan.error.message}</p>
  if (!s) return <p className="text-muted">Loading…</p>
  const order = ['fetch', 'sandbox', 'semgrep', 'gitleaks', 'osv', 'tests', 'record']

  return (
    <div className="space-y-6">
      <div className="text-sm"><Link to="/scans" className="text-muted hover:text-fg">← Scans</Link></div>
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Scan #{s.id} · {s.repository}</h1>
        <p className="text-sm text-muted">
          <span className={`font-medium ${STATUS_COLOR[s.status]}`}>{s.status}</span> · {s.trigger} · {timeAgo(s.created_at)}
          {s.commit_sha && <> · <span className="font-mono">{s.commit_sha.slice(0, 12)}</span></>} {duration(s) && `· ${duration(s)}`}
        </p>
        {s.error && <p className="mt-2 text-sm text-bad">{s.error}</p>}
      </header>

      <section>
        <h2 className="label">Steps</h2>
        <ol className="card divide-y divide-line">
          {order.filter((k) => s.steps[k]).map((k) => (
            <li key={k} className="flex flex-wrap gap-3 px-4 py-2.5 text-sm">
              <span className="w-20 font-medium">{k}</span>
              <span className={`w-16 ${STATUS_COLOR[s.steps[k].status] ?? ''}`}>{s.steps[k].status}</span>
              <span className="min-w-0 flex-1 break-words text-muted">{s.steps[k].detail}</span>
              {s.steps[k].duration_ms !== undefined && <span className="text-xs text-muted">{(s.steps[k].duration_ms! / 1000).toFixed(1)}s</span>}
            </li>
          ))}
        </ol>
      </section>

      {s.summary.findings !== undefined && (
        <section className="grid gap-3 sm:grid-cols-4">
          {[['Findings', s.summary.findings], ['New bugs', s.summary.new], ['Already known', s.summary.seen_again], ['Reopened', s.summary.reopened]].map(([l, v]) => (
            <div key={l as string} className="card p-4"><div className="label">{l}</div><div className="font-mono text-2xl tabular-nums">{v ?? 0}</div></div>
          ))}
        </section>
      )}
      {(s.summary.findings ?? 0) > 0 && <Link to={`/bugs?repository_id=${s.repository_id}&all=1`} className="btn-ghost">View bugs for this repository</Link>}

      {tests.data && tests.data.length > 0 && (
        <section className="space-y-3">
          <h2 className="label">Test results</h2>
          {tests.data.map((t) => (
            <div key={t.id} className="card">
              <div className="flex flex-wrap gap-4 border-b border-line px-4 py-3 text-sm">
                <code className="min-w-0 flex-1 break-all font-mono text-xs">{t.command}</code>
                <span className="text-ok">{t.passed} passed</span><span className="text-bad">{t.failed} failed</span>
                <span className="text-bad">{t.errors} errors</span><span className="text-muted">{t.skipped} skipped</span>
                {t.timed_out && <span className="text-bad">timed out</span>}
              </div>
              {t.results.filter((r) => r.status === 'failed' || r.status === 'error').map((r) => (
                <details key={r.test_id} className="border-b border-line px-4 py-2 text-sm">
                  <summary className="cursor-pointer"><span className="text-bad">✗</span> <span className="font-mono text-xs">{r.test_id}</span></summary>
                  <pre className="mt-2 overflow-x-auto font-mono text-xs text-muted">{r.message}</pre>
                </details>
              ))}
              {t.result_format === 'exit_code' && <p className="px-4 py-2 text-xs text-muted">No per-test report; result from exit code {t.exit_code}.</p>}
              <details className="px-4 py-2 text-sm"><summary className="cursor-pointer text-muted">Log</summary>
                <pre className="mt-2 max-h-96 overflow-auto font-mono text-xs">{t.log_tail}</pre></details>
            </div>
          ))}
        </section>
      )}
    </div>
  )
}
