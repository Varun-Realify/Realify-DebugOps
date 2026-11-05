import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, duration, SOURCE_LABEL, timeAgo, type DashboardSummary, type RunSummary } from '../api'
import { useEventStream } from '../realtime/useEventStream'

// Live via WebSocket; a slow poll is only a safety net.
const REFRESH_MS = 60000
const STAGE: Record<string, string> = {
  intake: 'Triaging', context: 'Preparing workspace', analysis: 'Analysing code', investigation: 'Investigating',
  root_cause: 'Confirming root cause', fix: 'Writing the fix', testing: 'Writing a regression test', validation: 'Running tests',
  security: 'Security check', review: 'Independent review', retry_gate: 'Retrying', report: 'Writing report',
  await_approval: 'Waiting for approval', open_pr: 'Opening pull request',
}
const OUTCOME: Record<string, { label: string; cls: string }> = {
  succeeded: { label: 'Fixed & merged', cls: 'bg-ok/15 text-ok' }, awaiting_merge: { label: 'PR awaiting developer', cls: 'bg-accent/15 text-accent' },
  waiting_approval: { label: 'Awaiting approval', cls: 'bg-warn/15 text-warn' }, running: { label: 'Running', cls: 'bg-accent/15 text-accent' },
  queued: { label: 'Queued', cls: 'bg-warn/15 text-warn' }, failed: { label: 'Failed', cls: 'bg-bad/15 text-bad' },
  needs_human: { label: 'Needs human', cls: 'bg-warn/15 text-warn' }, rejected: { label: 'Rejected', cls: 'bg-line text-muted' },
  cancelled: { label: 'Cancelled', cls: 'bg-line text-muted' }, closed: { label: 'PR closed', cls: 'bg-line text-muted' },
}

export function OutcomeBadge({ status }: { status: string }) {
  const o = OUTCOME[status] ?? { label: status, cls: 'bg-line text-muted' }
  return <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${o.cls}`}>{o.label}</span>
}

export default function Dashboard() {
  const { data, error } = useQuery({ queryKey: ['dashboard'], queryFn: () => api<DashboardSummary>('/dashboard/summary'), refetchInterval: REFRESH_MS })
  const runs = useQuery({ queryKey: ['runs'], queryFn: () => api<RunSummary[]>('/runs?limit=25'), refetchInterval: REFRESH_MS })
  const { events, connected } = useEventStream({ keep: 300 })
  const lastFor = (runId: number) => [...events].reverse().find((e) => e.kind === 'event' && e.run_id === runId)
  const live = (runs.data ?? []).filter((r) => ['queued', 'running'].includes(r.status))
  const t = data?.tasks as Record<string, number | null> | undefined
  const kpis: [string, string, string][] = [
    ['All tasks', String(t?.total ?? '–'), ''], ['In progress', String(t?.active ?? '–'), 'text-accent'],
    ['Awaiting developer', String(t?.awaiting_review ?? '–'), 'text-warn'], ['Fixed & merged', String(t?.succeeded ?? '–'), 'text-ok'],
    ['Failed / needs human', t ? String((t.failed ?? 0) + (t.needs_human ?? 0)) : '–', 'text-bad'],
    ['Success rate', t?.success_rate == null ? '–' : `${Math.round((t.success_rate as number) * 100)}%`, ''],
  ]

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
        <span className="text-xs">{error ? <span className="text-bad">Update failed: {error.message}</span>
          : <span className={connected ? 'text-ok' : 'text-warn'}>{connected ? '● Live' : 'Reconnecting…'}</span>}</span>
      </header>

      {data && data.errors_24h > 0 && (
        <Link to="/errors" className="card block border-bad p-3 text-sm text-bad">{data.errors_24h} error(s) in the last 24 hours. View system errors →</Link>
      )}

      <section aria-label="Debugging tasks" className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {kpis.map(([label, value, cls]) => (
          <div key={label} className="card p-4">
            <div className="text-xs text-muted">{label}</div>
            <div className={`mt-1 font-mono text-2xl tabular-nums ${cls}`}>{value}</div>
          </div>
        ))}
      </section>

      <section aria-labelledby="live-now">
        <h2 id="live-now" className="label">Live now</h2>
        {live.length === 0 ? (
          <div className="card p-5 text-sm text-muted">No AI developer is working right now. Start one from a bug (“Investigate with AI”) or <Link to="/bugs/new" className="text-accent">report a bug</Link>.</div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {live.map((r) => {
              const ev = lastFor(r.id)
              return (
                <Link key={r.id} to={`/runs/${r.id}`} className="card block p-4 hover:border-accent">
                  <div className="flex items-center gap-2 text-xs text-muted">
                    <span className="relative inline-flex h-2.5 w-2.5"><span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" /><span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-accent" /></span>
                    <span className="font-mono">{r.task_id}</span><span>· {r.repository}</span>{r.attempt > 0 && <span>· attempt {r.attempt}</span>}
                  </div>
                  <div className="mt-1 font-medium">{r.task_title}</div>
                  <div className="mt-1 text-sm text-accent">{r.current_node ? STAGE[r.current_node] ?? r.current_node : 'Queued'}…</div>
                  {ev?.message && <div className="mt-2 truncate rounded bg-[#0b0f14] px-2 py-1 font-mono text-xs text-[#d6dde8]">{ev.message}</div>}
                </Link>
              )
            })}
          </div>
        )}
      </section>

      <section className="card" aria-labelledby="recent-tasks">
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <h2 id="recent-tasks" className="text-sm font-medium">Debugging tasks</h2>
          <Link to="/runs" className="text-xs text-accent hover:underline">All tasks</Link>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px]">
            <thead><tr><th className="th">Task</th><th className="th">Repository</th><th className="th">Outcome</th><th className="th">Validation</th><th className="th">Time</th><th className="th">PR</th><th className="th">Started</th></tr></thead>
            <tbody>
              {runs.data?.length === 0 && <tr><td className="td py-8 text-center text-muted" colSpan={7}>No debugging tasks yet.</td></tr>}
              {runs.data?.slice(0, 12).map((r) => (
                <tr key={r.id}>
                  <td className="td"><Link to={`/runs/${r.id}`} className="hover:text-accent"><span className="font-mono text-xs text-muted">{r.task_id}</span> {r.task_title}</Link></td>
                  <td className="td text-sm text-muted">{r.repository}</td>
                  <td className="td"><OutcomeBadge status={r.status} /></td>
                  <td className={`td text-sm ${r.validation_verdict === 'verified' ? 'text-ok' : r.validation_verdict ? 'text-warn' : 'text-muted'}`}>{r.validation_verdict ?? '–'}</td>
                  <td className="td text-sm text-muted">{duration(r.execution_seconds)}</td>
                  <td className="td text-sm">{r.pr_url ? <a className="text-accent hover:underline" href={r.pr_url} target="_blank" rel="noreferrer">PR</a> : '–'}</td>
                  <td className="td text-sm text-muted">{timeAgo(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="card p-4" aria-labelledby="bugs">
          <div className="mb-3 flex items-baseline justify-between"><h2 id="bugs" className="label mb-0">Open bugs</h2><Link to="/bugs" className="text-xs text-accent hover:underline">View</Link></div>
          <div className="grid grid-cols-5 gap-1 text-center">
            {(['critical', 'high', 'medium', 'low', 'info'] as const).map((s) => (
              <Link key={s} to={`/bugs?severity=${s}`} className="rounded-md p-1 hover:bg-bg">
                <div className={`font-mono text-xl tabular-nums ${s === 'critical' || s === 'high' ? 'text-bad' : s === 'medium' ? 'text-warn' : ''}`}>{data ? data.open_bugs_by_severity[s] : '–'}</div>
                <div className="text-[11px] capitalize text-muted">{s}</div>
              </Link>
            ))}
          </div>
          {data && Object.keys(data.open_bugs_by_source).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-x-3 gap-y-1 border-t border-line pt-3 text-xs text-muted">
              {Object.entries(data.open_bugs_by_source).map(([k, v]) => <span key={k}>{SOURCE_LABEL[k] ?? k}: <b className="text-fg">{v}</b></span>)}
            </div>
          )}
        </section>
        <section className="card p-4" aria-labelledby="agents">
          <h2 id="agents" className="label">Agent steps</h2>
          <dl className="grid grid-cols-4 gap-2 text-center">
            {data && ([['running', 'text-accent'], ['waiting', 'text-warn'], ['completed', 'text-ok'], ['failed', 'text-bad']] as const).map(([k, c]) => (
              <div key={k}><dd className={`font-mono text-xl tabular-nums ${c}`}>{data.agent_status[k]}</dd><dt className="text-[11px] capitalize text-muted">{k}{k === 'completed' || k === 'failed' ? ' 24h' : ''}</dt></div>
            ))}
          </dl>
          <h2 className="label mt-4">Latest scan tests</h2>
          <dl className="grid grid-cols-4 gap-2 text-center">
            {data && ([['passed', 'text-ok'], ['failed', 'text-bad'], ['errors', 'text-bad'], ['skipped', 'text-muted']] as const).map(([k, c]) => (
              <div key={k}><dd className={`font-mono text-xl tabular-nums ${c}`}>{data.latest_tests[k]}</dd><dt className="text-[11px] capitalize text-muted">{k}</dt></div>
            ))}
          </dl>
        </section>
        <section className="card" aria-labelledby="system">
          <h2 id="system" className="border-b border-line px-4 py-3 text-sm font-medium">System</h2>
          <ul className="divide-y divide-line text-sm">
            {data?.workers.filter((w) => w.online).map((w) => (
              <li key={w.id} className="flex items-center gap-2 px-4 py-2"><span className="h-2 w-2 rounded-full bg-ok" /><span className="truncate font-mono text-xs">{w.id}</span><span className="ml-auto text-xs text-muted">{timeAgo(w.last_seen_at)}</span></li>
            ))}
            {data && !data.workers.some((w) => w.online) && <li className="px-4 py-3 text-bad">No worker online: tasks will not run.</li>}
            {data?.recent_scans.slice(0, 3).map((sc) => (
              <li key={sc.id}><Link to={`/scans/${sc.id}`} className="flex gap-2 px-4 py-2 hover:bg-bg"><span className="text-muted">scan</span><span className="min-w-0 flex-1 truncate">{sc.repository}</span><span className="text-xs">{sc.status}</span></Link></li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
