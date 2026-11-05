import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, Route, Routes, useParams } from 'react-router-dom'
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api, duration, RUN_STATUS_COLOR, TASK_TYPE, timeAgo, type RunDetail, type RunSummary, type User } from '../api'
import { useEventStream, type LiveEvent } from '../realtime/useEventStream'

const LABEL: Record<string, string> = {
  intake: 'Intake · classification', aws_investigation: 'AWS DevOps Agent', context: 'Context collection', analysis: 'Code Analyst', investigation: 'Investigation',
  root_cause: 'Root cause gate', fix: 'Fix agent', testing: 'Testing agent', validation: 'Validation (platform)',
  security: 'Security agent', review: 'Review agent', retry_gate: 'Retry gate', report: 'Report',
  await_approval: 'Human approval', open_pr: 'Pull request',
}
const POS: Record<string, [number, number]> = {
  intake: [0, 0], aws_investigation: [0, 1], context: [1, 0], analysis: [2, 0], investigation: [3, 0], root_cause: [4, 0], fix: [5, 0], testing: [6, 0],
  validation: [6, 1], security: [5, 1], review: [4, 1], retry_gate: [5, 2], report: [3, 1], await_approval: [2, 1], open_pr: [1, 1],
}
const EDGES: [string, string, boolean?][] = [
  ['intake', 'aws_investigation'], ['aws_investigation', 'context'], ['context', 'analysis'], ['analysis', 'investigation'], ['investigation', 'root_cause'], ['root_cause', 'fix'],
  ['fix', 'testing'], ['testing', 'validation'], ['validation', 'security'], ['security', 'review'], ['review', 'report'],
  ['report', 'await_approval'], ['await_approval', 'open_pr'],
  ['validation', 'retry_gate', true], ['security', 'retry_gate', true], ['review', 'retry_gate', true], ['fix', 'retry_gate', true],
  ['retry_gate', 'fix', true], ['retry_gate', 'report', true], ['root_cause', 'report', true], ['validation', 'report', true],
]
const NODE_STYLE: Record<string, string> = {
  completed: 'var(--ok)', running: 'var(--accent)', waiting: 'var(--warn)', failed: 'var(--bad)',
}

export default function Runs({ me }: { me: User }) {
  return (
    <Routes>
      <Route index element={<RunList />} />
      <Route path=":id" element={<RunView me={me} />} />
    </Routes>
  )
}

function testsLabel(t: RunSummary['test_summary']) {
  if (!t || t.passed === undefined) return '–'
  return `${t.passed} passed${t.failed ? `, ${t.failed} failed` : ''}${t.error ? `, ${t.error} errors` : ''}`
}

function RunList() {
  const q = useQuery({ queryKey: ['runs'], queryFn: () => api<RunSummary[]>('/runs') })
  useEventStream({ keep: 1 })
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Debugging tasks</h1>
        <a className="btn-ghost" href="/api/v1/runs/export.csv">Export audit trail (CSV)</a>
      </div>
      <p className="text-sm text-muted">Every run's audit record is stored in PostgreSQL. Start a run from a bug ("Investigate with AI") or report a new bug.</p>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[1100px]">
          <thead><tr><th className="th">Task</th><th className="th">Repository / branch</th><th className="th">Stage</th>
            <th className="th">Files changed</th><th className="th">Tests</th><th className="th">Time</th><th className="th">PR</th><th className="th">Status</th></tr></thead>
          <tbody>
            {q.data?.length === 0 && <tr><td className="td py-8 text-center text-muted" colSpan={8}>No runs yet.</td></tr>}
            {q.data?.map((r) => (
              <tr key={r.id} className="align-top">
                <td className="td"><Link to={String(r.id)} className="font-medium hover:text-accent"><span className="font-mono text-xs text-muted">{r.task_id}</span> {r.task_title}</Link>
                  {r.task_type && <span className={`ml-2 rounded-full px-1.5 py-0.5 text-[11px] ${TASK_TYPE[r.task_type]?.cls ?? ''}`}>{TASK_TYPE[r.task_type]?.label}</span>}
                  <div className="text-xs text-muted">{timeAgo(r.created_at)}</div></td>
                <td className="td text-xs"><div>{r.repository}</div><div className="font-mono text-muted">{r.branch_name}</div>
                  {r.base_sha && <div className="font-mono text-muted">base {r.base_sha.slice(0, 10)}</div>}</td>
                <td className="td text-sm">{r.current_node ? LABEL[r.current_node] ?? r.current_node : '–'}{r.attempt > 0 && <span className="text-muted"> · attempt {r.attempt}</span>}</td>
                <td className="td font-mono text-xs">{r.files_changed.slice(0, 3).join(', ')}{r.files_changed.length > 3 ? ` +${r.files_changed.length - 3}` : ''}{!r.files_changed.length && '–'}</td>
                <td className="td text-sm">{testsLabel(r.test_summary)}</td>
                <td className="td text-sm text-muted">{duration(r.execution_seconds)}</td>
                <td className="td text-sm">{r.pr_url ? <a className="text-accent hover:underline" href={r.pr_url} target="_blank" rel="noreferrer">PR</a> : '–'}</td>
                <td className={`td text-sm font-medium ${RUN_STATUS_COLOR[r.status] ?? ''}`}>{r.status.replace('_', ' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {q.error && <p className="p-4 text-sm text-bad">{q.error.message}</p>}
      </div>
    </div>
  )
}

function Delivery({ pr }: { pr: NonNullable<RunDetail['pull_request']> }) {
  const color = (v?: string | null) => v === 'success' || v === 'approved' || v === 'healthy' || v === 'merged' ? 'text-ok'
    : v === 'failure' || v === 'changes_requested' || v === 'unhealthy' || v === 'error' ? 'text-bad' : 'text-warn'
  const prod = pr.deployments.filter((d) => d.state)
  const steps: [string, string, string | null | undefined][] = [
    ['1. AI opened PR', `#${pr.number}`, 'success'],
    ['2–3. Developer review', pr.review_state ?? 'awaiting review', pr.review_state],
    ['3. Merge', pr.state === 'merged' ? `merged ${pr.merge_commit_sha?.slice(0, 10) ?? ''}` : pr.state, pr.state === 'merged' ? 'merged' : pr.state === 'closed' ? 'failure' : null],
    ['4–5. CI / build on main', pr.post_merge_ci_status ?? (pr.state === 'merged' ? 'waiting' : '–'), pr.post_merge_ci_status],
    ['6. Deployment (your CD)', prod.length ? prod.map((d) => `${d.environment}: ${d.state}`).join(', ') : (pr.state === 'merged' ? 'none reported yet' : '–'),
      prod.some((d) => d.state === 'success') ? 'success' : prod.some((d) => ['failure', 'error'].includes(d.state)) ? 'failure' : null],
    ['7. Production health', pr.health?.status ?? (pr.state === 'merged' ? 'no health check configured' : '–'), pr.health?.status],
  ]
  return (
    <section className="card p-4 text-sm">
      <h2 className="label">Delivery: PR → review → merge → CI → deploy → health</h2>
      <ol className="space-y-1">
        {steps.map(([k, v, st]) => <li key={k} className="flex gap-3"><span className="w-48 shrink-0 text-muted">{k}</span><span className={st ? color(st) : ''}>{v}</span></li>)}
      </ol>
      {pr.checks.length > 0 && <p className="mt-2 text-xs text-muted">PR checks: {pr.checks.map((c) => `${c.name}: ${c.conclusion ?? c.status}`).join(' · ')}</p>}
      {pr.post_merge_checks.length > 0 && <p className="text-xs text-muted">Main checks: {pr.post_merge_checks.map((c) => `${c.name}: ${c.conclusion ?? c.status}`).join(' · ')}</p>}
      {pr.health?.bug_id && <p className="mt-2 text-bad">Regression detected: <Link className="underline" to={`/bugs/${pr.health.bug_id}`}>bug #{pr.health.bug_id}</Link></p>}
      {pr.health?.recent && pr.health.recent.length > 0 && <p className="mt-1 font-mono text-xs text-muted">
        {pr.health.recent.map((c) => (c.ok ? '●' : '✕')).join(' ')}</p>}
    </section>
  )
}

function WorkflowGraph({ run }: { run: RunDetail }) {
  const { nodes, edges } = useMemo(() => {
    const nodes: Node[] = run.node_order.map((n) => {
      const st = run.nodes[n]?.status
      const [x, y] = POS[n] ?? [0, 3]
      // Row 0 flows left->right, row 1 flows back right->left, the retry gate sits below.
      const sides = y === 0 ? { sourcePosition: Position.Right, targetPosition: Position.Left }
        : y === 1 ? { sourcePosition: Position.Left, targetPosition: Position.Right }
        : { sourcePosition: Position.Top, targetPosition: Position.Bottom }
      return {
        id: n, position: { x: x * 180, y: y * 120 }, draggable: false, connectable: false, ...sides,
        data: { label: (
          <div className="text-left">
            <div className="text-xs font-medium">{LABEL[n] ?? n}</div>
            <div className="text-[10px] text-muted">{st ?? 'not run'}{run.nodes[n]?.attempt ? ` · #${run.nodes[n].attempt}` : ''}</div>
          </div>) },
        style: { width: 150, background: 'var(--panel)', color: 'var(--fg)', borderRadius: 8, padding: 8,
          border: `2px solid ${st ? NODE_STYLE[st] ?? 'var(--line)' : 'var(--line)'}`, opacity: st ? 1 : 0.55,
          boxShadow: st === 'running' ? '0 0 0 4px color-mix(in srgb, var(--accent) 25%, transparent)' : undefined },
      }
    })
    const edges: Edge[] = EDGES.map(([a, b, loop]) => ({
      id: `${a}-${b}`, source: a, target: b, animated: run.nodes[a]?.status === 'running',
      style: { stroke: 'var(--muted)', strokeWidth: loop ? 1 : 1.75, strokeDasharray: loop ? '4 4' : undefined, opacity: loop ? 0.6 : 1 },
      markerEnd: { type: MarkerType.ArrowClosed },
    }))
    return { nodes, edges }
  }, [run])
  return (
    <div className="card h-[420px]" role="img" aria-label="Workflow execution graph">
      <ReactFlow nodes={nodes} edges={edges} fitView colorMode="system" proOptions={{ hideAttribution: true }} nodesConnectable={false}>
        <Background /><Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}

function Diff({ runId, attemptId }: { runId: number; attemptId: number }) {
  const q = useQuery({ queryKey: ['diff', runId, attemptId], queryFn: async () => {
    const r = await fetch(`/api/v1/runs/${runId}/attempts/${attemptId}/diff`, { credentials: 'same-origin' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.text()
  } })
  if (q.error) return <p className="text-sm text-bad">{q.error.message}</p>
  return (
    <pre className="card max-h-[600px] overflow-auto p-3 font-mono text-xs leading-relaxed">
      {(q.data ?? '').split('\n').map((l, i) => (
        <div key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'text-ok' : l.startsWith('-') && !l.startsWith('---') ? 'text-bad' : l.startsWith('@@') ? 'text-accent' : ''}>{l || ' '}</div>
      ))}
    </pre>
  )
}

const ORDER = ['intake', 'aws_investigation', 'context', 'analysis', 'investigation', 'root_cause', 'fix', 'testing', 'validation', 'security', 'review', 'report', 'await_approval', 'open_pr']
const AGENT_COLOR: Record<string, string> = {
  bug_hunter: '#f59e0b', code_analyst: '#38bdf8', investigation: '#a78bfa', fix: '#34d399', testing: '#f472b6',
  security: '#fb7185', review: '#facc15', aws_devops_agent: '#ff9900', alerts: '#ff9900', platform: '#94a3b8', workflow: '#94a3b8', github: '#cbd5e1', delivery: '#22d3ee',
}
const AGENT_NAME: Record<string, string> = {
  bug_hunter: 'Bug Hunter', code_analyst: 'Code Analyst', investigation: 'Investigator', fix: 'Fixer', testing: 'Tester',
  security: 'Security', review: 'Reviewer', aws_devops_agent: 'AWS Agent', alerts: 'CloudWatch', platform: 'DebugOps', workflow: 'DebugOps', github: 'GitHub', delivery: 'Delivery',
}
const KIND_ICON: Record<string, string> = {
  finding: '🎯', file_read: '📖', edit: '✏️', command: '❯', search: '🔎', thought: '💭', test: '🧪', error: '✕', action: '▸', info: '▸',
}
const ACTIVE = ['queued', 'running']

function StageIcon({ st }: { st?: string }) {
  if (st === 'completed') return <span className="text-ok">✓</span>
  if (st === 'running') return <span className="relative inline-flex h-2.5 w-2.5"><span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" /><span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-accent" /></span>
  if (st === 'waiting') return <span className="text-warn">⏸</span>
  if (st === 'failed') return <span className="text-bad">✕</span>
  if (st === 'skipped') return <span className="text-muted" title="skipped">↷</span>
  return <span className="text-muted">○</span>
}

/** Terminal-style live feed: every read, edit, command, test and thought of the agents, as it happens. */
function LiveFeed({ events, running }: { events: LiveEvent[]; running: boolean }) {
  const box = useRef<HTMLDivElement>(null)
  const [follow, setFollow] = useState(true)
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight }, [events, follow])
  return (
    <div className="relative overflow-hidden rounded-lg border border-line bg-[#0b0f14] text-[#d6dde8]">
      <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2 text-xs text-slate-400">
        <span className="h-2.5 w-2.5 rounded-full bg-[#ff5f57]" /><span className="h-2.5 w-2.5 rounded-full bg-[#febc2e]" /><span className="h-2.5 w-2.5 rounded-full bg-[#28c840]" />
        <span className="ml-2">live agent session</span>
        {!follow && <button className="ml-auto rounded bg-white/10 px-2 py-0.5 text-slate-200" onClick={() => setFollow(true)}>↓ follow</button>}
      </div>
      <div ref={box} onScroll={(e) => { const el = e.currentTarget; setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40) }}
        className="h-[460px] overflow-y-auto px-3 py-2 font-mono text-[12.5px] leading-relaxed" aria-live="polite" role="log">
        {events.length === 0 && <p className="text-slate-500">{running ? 'Waiting for the first agent to start…' : 'No activity recorded.'}</p>}
        {events.map((e) => {
          const src = e.source ?? 'platform'
          const kind = e.level === 'error' ? 'error' : (e.type ?? 'info')
          return (
            <div key={e.id} className={`flex gap-2 ${kind === 'thought' ? 'italic text-slate-400' : ''} ${e.level === 'error' ? 'text-[#ff7b72]' : ''}`}>
              <span className="w-16 shrink-0 text-slate-600">{e.ts ? new Date(e.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : ''}</span>
              <span className="w-24 shrink-0 truncate" style={{ color: AGENT_COLOR[src] ?? '#94a3b8' }}>{AGENT_NAME[src] ?? src}</span>
              <span className="w-5 shrink-0 text-center">{KIND_ICON[kind] ?? '▸'}</span>
              <span className={`min-w-0 break-words ${kind === 'command' ? 'text-[#7ee787]' : kind === 'edit' ? 'text-[#ffa657]' : ''}`}>{e.message}</span>
            </div>
          )
        })}
        {running && <div className="mt-1 text-slate-500"><span className="inline-block h-3.5 w-2 animate-pulse bg-slate-400 align-middle" /></div>}
      </div>
    </div>
  )
}

function RunView({ me }: { me: User }) {
  const { id } = useParams()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['run', id], queryFn: () => api<RunDetail>(`/runs/${id}`) })
  const tests = useQuery({ queryKey: ['run-tests', id, q.data?.nodes?.validation?.status], queryFn: () => api<any[]>(`/runs/${id}/tests`) })
  const { events, connected } = useEventStream({ runId: Number(id), keep: 1500 })
  const [tab, setTab] = useState('code changes')
  const [attempt, setAttempt] = useState<number | null>(null)
  const [reason, setReason] = useState('')
  const refresh = () => qc.invalidateQueries({ queryKey: ['run', id] })
  const decide = useMutation({ mutationFn: (d: 'approve' | 'reject') => api(`/runs/${id}/${d}`, { method: 'POST', body: { reason } }), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: () => api(`/runs/${id}/cancel`, { method: 'POST' }), onSuccess: refresh })
  const refreshPr = useMutation({ mutationFn: (prId: number) => api(`/pull-requests/${prId}/refresh`, { method: 'POST' }), onSuccess: refresh })
  const r = q.data
  if (q.error) return <p className="text-bad">{q.error.message}</p>
  if (!r) return <p className="text-muted">Loading…</p>
  const running = ACTIVE.includes(r.status)
  const canApprove = r.status === 'waiting_approval' && me.role !== 'developer' && me.id !== r.started_by
  const canCancel = me.role !== 'reviewer' && ['queued', 'running', 'waiting_approval'].includes(r.status)
  const current = r.attempts.find((a) => a.id === attempt) ?? r.attempts[r.attempts.length - 1]
  const last = [...events].reverse().find((e) => e.kind === 'event')
  const agentNow = r.current_node ? (LABEL[r.current_node] ?? r.current_node) : null
  const tabs = ['code changes', 'tests', 'security & review', 'report', 'workflow map']

  return (
    <div className="space-y-5">
      <div className="text-sm"><Link to="/runs" className="text-muted hover:text-fg">← Tasks</Link></div>
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded bg-bg px-2 py-0.5 font-mono text-xs text-muted">{r.task_id}</span>
          {r.task_type && <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${TASK_TYPE[r.task_type]?.cls ?? ''}`}>{TASK_TYPE[r.task_type]?.label ?? r.task_type}</span>}
          <span className={`rounded px-2 py-0.5 text-xs font-medium ${RUN_STATUS_COLOR[r.status] ?? ''} border border-current`}>{r.status.replace('_', ' ')}</span>
          <span className={`text-xs ${connected ? 'text-ok' : 'text-warn'}`}>{connected ? '● live' : 'reconnecting…'}</span>
          {canCancel && <button className="btn-ghost ml-auto" onClick={() => cancel.mutate()}>Cancel</button>}
        </div>
        <h1 className="text-xl font-semibold tracking-tight"><Link to={`/bugs/${r.bug_id}`} className="hover:text-accent">{r.task_title}</Link></h1>
        {running && (
          <p className="flex items-center gap-2 text-sm">
            <StageIcon st="running" />
            <span className="font-medium">{agentNow ?? 'Starting'}</span>
            {last?.message && <span className="truncate text-muted">— {last.message}</span>}
          </p>
        )}
        {r.error && <p className="text-sm text-bad">{r.error}</p>}
      </header>

      <div className="grid gap-5 xl:grid-cols-[220px_minmax(0,1fr)_280px]">
        <nav aria-label="Stages" className="card h-fit p-3">
          <h2 className="label">Stages</h2>
          <ol className="space-y-1.5 text-sm">
            {ORDER.map((n, i) => {
              // A stage with no status while a later stage ran was skipped (e.g. approval gate turned off).
              const st = r.nodes[n]?.status ?? (ORDER.slice(i + 1).some((m) => r.nodes[m]?.status) ? 'skipped' : undefined)
              return (
                <li key={n} className={`flex items-center gap-2 ${st ? '' : 'text-muted'} ${st === 'running' ? 'font-medium' : ''}`}>
                  <span className="flex w-4 justify-center"><StageIcon st={st} /></span>
                  <span className="truncate">{LABEL[n] ?? n}</span>
                  {r.nodes[n]?.attempt ? <span className="ml-auto text-xs text-muted">#{r.nodes[n].attempt}</span> : null}
                </li>
              )
            })}
          </ol>
        </nav>

        <LiveFeed events={events.filter((e) => e.kind === 'event')} running={running} />

        <aside className="space-y-4">
          {r.investigation && (
            <section className="card space-y-2 p-4 text-sm" aria-labelledby="aws-inv">
              <h2 id="aws-inv" className="label">AWS DevOps Agent investigation</h2>
              <p><b className={r.investigation.status === 'COMPLETED' ? 'text-ok' : ['FAILED', 'ERROR', 'TIMED_OUT'].includes(r.investigation.status) ? 'text-bad' : 'text-accent'}>
                {r.investigation.status.toLowerCase().replace('_', ' ')}</b> · {r.investigation.records} journal record(s)</p>
              {r.investigation.agent_task_id && <p className="break-all font-mono text-xs text-muted">task {r.investigation.agent_task_id}</p>}
              {r.investigation.error && <p className="text-xs text-warn">{r.investigation.error}</p>}
              {r.investigation.key_records.slice(-4).map((k, i) => <p key={i} className="text-xs"><b>{k.type}</b> {k.text.slice(0, 300)}</p>)}
              {r.investigation.recommendations.length > 0 && (
                <div className="rounded border border-warn p-2 text-xs">
                  <div className="font-medium text-warn">Recommendations (never executed by DebugOps; review before acting)</div>
                  <ul className="mt-1 list-disc pl-4">{r.investigation.recommendations.map((x, i) => <li key={i}>{x.title ?? x.recommendationId}{x.priority && <span className="text-muted"> ({x.priority.toLowerCase()})</span>}{x.content?.summary && <div className="text-muted">{x.content.summary}</div>}</li>)}</ul>
                </div>)}
            </section>
          )}
          {r.status === 'waiting_approval' && (
            <div className="card space-y-2 border-warn p-4 text-sm">
              <div className="font-medium">Approve opening the PR?</div>
              <p className="text-xs text-muted">{r.report?.test_summary?.reason}</p>
              {canApprove ? (<>
                <input className="input" placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Decision reason" />
                <div className="flex gap-2">
                  <button className="btn-primary" disabled={decide.isPending} onClick={() => decide.mutate('approve')}>Approve</button>
                  <button className="btn-ghost" disabled={decide.isPending} onClick={() => decide.mutate('reject')}>Reject</button>
                </div>
              </>) : <p className="text-xs text-muted">{me.id === r.started_by ? 'Another Reviewer or Admin must approve.' : 'Reviewers and Admins approve.'}</p>}
              {decide.error && <p role="alert" className="text-bad">{decide.error.message}</p>}
            </div>
          )}
          {r.pull_request && (
            <div className="space-y-3">
              <div className="card flex flex-wrap items-center gap-2 p-3 text-sm">
                <a className="font-medium text-accent hover:underline" href={r.pull_request.url} target="_blank" rel="noreferrer">PR #{r.pull_request.number}</a>
                <span className="text-muted">{r.pull_request.state}</span>
                {me.role !== 'reviewer' && <button className="btn-ghost ml-auto px-2 py-1 text-xs" onClick={() => refreshPr.mutate(r.pull_request!.id)}>Refresh</button>}
              </div>
              <Delivery pr={r.pull_request} />
            </div>
          )}
          <dl className="card space-y-2 p-4 text-sm">
            {([['Repository', r.repository], ['Branch', r.branch_name], ['Base commit', r.base_sha?.slice(0, 12)],
              ['Validation', r.validation_verdict ?? '–'], ['Fix attempts', String(r.attempt)], ['Tests', testsLabel(r.test_summary)],
              ['Active time', duration(r.execution_seconds)], ['AI usage', `${(r.tokens_in + r.tokens_out).toLocaleString()} tokens · $${r.cost_usd.toFixed(2)}`]] as [string, string | null | undefined][])
              .map(([k, v]) => <div key={k}><dt className="text-xs text-muted">{k}</dt><dd className="break-all font-mono text-xs">{v || '–'}</dd></div>)}
          </dl>
        </aside>
      </div>

      <div className="flex flex-wrap items-center gap-1 border-b border-line" role="tablist">
        {tabs.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm capitalize ${tab === t ? 'border-accent font-medium' : 'border-transparent text-muted hover:text-fg'}`}>{t}</button>
        ))}
      </div>

      {(tab === 'security & review' || tab === 'code changes') && r.attempts.length > 0 && (
        <div className="flex flex-wrap gap-2 text-sm">
          {r.attempts.map((a) => (
            <button key={a.id} className={a.id === current?.id ? 'btn-primary' : 'btn-ghost'} onClick={() => setAttempt(a.id)}>
              Attempt {a.attempt_no} · {a.validation.verdict ?? 'validating'}{a.status === 'accepted' ? ' ✓' : ''}
            </button>
          ))}
        </div>
      )}
      {tab === 'code changes' && (current ? (<>
        <p className="text-xs text-muted">Source: {current.fix_paths.join(', ') || '–'} · Tests: {current.test_paths.join(', ') || '–'}</p>
        <Diff runId={r.id} attemptId={current.id} />
      </>) : <p className="text-sm text-muted">{running ? 'Code changes appear here once the Fix and Testing agents have finished an attempt.' : 'No code changes.'}</p>)}

      {tab === 'tests' && (
        <div className="space-y-3">
          {(tests.data ?? []).length === 0 && <p className="text-sm text-muted">No validation test runs yet.</p>}
          {tests.data?.map((t) => (
            <div key={t.id} className="card p-4 text-sm">
              <div className="flex flex-wrap gap-4">
                <span className="font-medium">{t.phase === 'baseline' ? 'Baseline (original code)' : t.phase === 'base' ? `Original code + new tests (attempt ${t.attempt_no})` : `With fix (attempt ${t.attempt_no})`}</span>
                <span className="text-ok">{t.passed} passed</span><span className="text-bad">{t.failed} failed</span><span className="text-bad">{t.errors} errors</span><span className="text-muted">{t.skipped} skipped</span>
              </div>
              {t.regression.length > 0 && <ul className="mt-2 text-xs">{t.regression.map((x: any) => <li key={x.test_id}>Regression test <span className="font-mono">{x.test_id}</span>: {x.status}</li>)}</ul>}
              <details className="mt-2"><summary className="cursor-pointer text-muted">Log</summary><pre className="mt-2 max-h-80 overflow-auto font-mono text-xs">{t.log_tail}</pre></details>
            </div>
          ))}
        </div>
      )}

      {tab === 'security & review' && (current ? (
        <div className="grid gap-4 lg:grid-cols-3">
          <section className="card p-4 text-sm"><h2 className="label">Validation (platform)</h2><p><b>{current.validation.verdict ?? '–'}</b>: {current.validation.reason}</p></section>
          <section className="card p-4 text-sm"><h2 className="label">Security</h2><p><b>{current.security.verdict ?? '–'}</b> {current.security.agent?.summary}</p>
            <ul className="mt-2 space-y-1">
              {(current.security.introduced_findings ?? []).map((f, i) => <li key={i}>{f.severity}: {f.title} ({f.file}:{f.line})</li>)}
              {(current.security.agent?.issues ?? []).map((f, i) => <li key={`a${i}`}>{f.severity}: {f.file}:{f.line} {f.description}</li>)}
            </ul></section>
          <section className="card p-4 text-sm"><h2 className="label">Independent review</h2><p><b>{current.review.verdict ?? '–'}</b> {current.review.summary}</p>
            <ul className="mt-2 space-y-1">{(current.review.issues ?? []).map((f, i) => <li key={i}>{f.severity}: {f.file}:{f.line} {f.description}</li>)}</ul></section>
        </div>) : <p className="text-sm text-muted">No attempts yet.</p>)}

      {tab === 'report' && (r.report ? (
        <div className="space-y-4 text-sm">
          <section className="card p-4"><h2 className="label">Root cause</h2><p>{r.report.root_cause || '—'}</p>
            <ul className="mt-2 space-y-1 font-mono text-xs">{r.report.evidence.map((e, i) => <li key={i}>{e.file}:{e.line} <span className="font-sans text-muted">{e.explanation}</span></li>)}</ul></section>
          <section className="card p-4"><h2 className="label">Affected files</h2>
            <ul className="space-y-1 font-mono text-xs">{r.report.affected.map((a) => <li key={a.path}>{a.path} <span className="font-sans text-muted">({a.kind}) {a.ranges.map(([x, y]) => x === y ? `L${x}` : `L${x}-L${y}`).join(', ')}</span></li>)}</ul></section>
          <details className="card p-4"><summary className="cursor-pointer">Full report (Markdown, as published to GitHub)</summary>
            <button className="btn-ghost mt-2" onClick={() => navigator.clipboard.writeText(r.report!.markdown)}>Copy</button>
            <pre className="mt-2 max-h-[600px] overflow-auto whitespace-pre-wrap font-mono text-xs">{r.report.markdown}</pre></details>
        </div>
      ) : <p className="text-sm text-muted">The report is written when the analysis finishes.</p>)}

      {tab === 'workflow map' && <WorkflowGraph run={r} />}
    </div>
  )
}
