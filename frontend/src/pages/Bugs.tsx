import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, RUN_STATUS_COLOR, SEVERITY_COLOR, SOURCE_LABEL, timeAgo, type Bug, type BugDetail, type Repository, type RunSummary, type User } from '../api'

const STATUSES = ['new', 'triaged', 'investigating', 'fix_proposed', 'pr_open', 'resolved', 'wont_fix', 'closed']

export function SeverityBadge({ s }: { s: string }) {
  return <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium uppercase ${SEVERITY_COLOR[s] ?? ''}`}>{s}</span>
}

export default function Bugs({ me }: { me: User }) {
  return (
    <Routes>
      <Route index element={<BugList canReport={me.role !== 'reviewer'} />} />
      <Route path=":id" element={<BugView canEdit={me.role !== 'reviewer'} />} />
    </Routes>
  )
}

function BugList({ canReport }: { canReport: boolean }) {
  const [params, setParams] = useSearchParams()
  const filters = Object.fromEntries(params.entries())
  const set = (k: string, v: string) => { const n = new URLSearchParams(params); if (v) n.set(k, v); else n.delete(k); setParams(n) }
  const qs = new URLSearchParams({ ...filters, ...(filters.status_ ? {} : { open_only: filters.all ? 'false' : 'true' }) })
  qs.delete('all')
  const bugs = useQuery({ queryKey: ['bugs', qs.toString()], queryFn: () => api<Bug[]>(`/bugs?${qs}`) })
  const repos = useQuery({ queryKey: ['repos'], queryFn: () => api<Repository[]>('/repositories') })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Bugs</h1>
        {canReport && <Link to="/bugs/new" className="btn-primary">Report bug</Link>}
      </div>
      <div className="flex flex-wrap gap-2">
        <input className="input w-56" placeholder="Search…" defaultValue={filters.q ?? ''}
          onKeyDown={(e) => e.key === 'Enter' && set('q', (e.target as HTMLInputElement).value)} aria-label="Search bugs" />
        <select className="input w-auto" value={filters.repository_id ?? ''} onChange={(e) => set('repository_id', e.target.value)} aria-label="Repository">
          <option value="">All repositories</option>
          {repos.data?.map((r) => <option key={r.id} value={r.id}>{r.full_name}</option>)}
        </select>
        <select className="input w-auto" value={filters.severity ?? ''} onChange={(e) => set('severity', e.target.value)} aria-label="Severity">
          <option value="">Any severity</option>
          {['critical', 'high', 'medium', 'low', 'info'].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select className="input w-auto" value={filters.source ?? ''} onChange={(e) => set('source', e.target.value)} aria-label="Source">
          <option value="">Any source</option>
          {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select className="input w-auto" value={filters.status_ ?? (filters.all ? 'all' : '')}
          onChange={(e) => { const n = new URLSearchParams(params); n.delete('status_'); n.delete('all');
            if (e.target.value === 'all') n.set('all', '1'); else if (e.target.value) n.set('status_', e.target.value); setParams(n) }} aria-label="Status">
          <option value="">Open</option>
          <option value="all">All statuses</option>
          {[...STATUSES, 'duplicate'].map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
        </select>
      </div>

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[760px]">
          <thead><tr><th className="th">Bug</th><th className="th">Severity</th><th className="th">Source</th><th className="th">Status</th><th className="th">Last seen</th></tr></thead>
          <tbody>
            {bugs.data?.length === 0 && <tr><td className="td py-8 text-center text-muted" colSpan={5}>No bugs match these filters.</td></tr>}
            {bugs.data?.map((b) => (
              <tr key={b.id}>
                <td className="td">
                  <Link to={String(b.id)} className="font-medium hover:text-accent">{b.title}</Link>
                  <div className="text-xs text-muted">
                    {b.repository}
                    {b.affected_files[0] && <> · <span className="font-mono">{b.affected_files[0].path}{b.affected_files[0].line_start ? `:${b.affected_files[0].line_start}` : ''}</span></>}
                    {b.github_issue_number && <> · #{b.github_issue_number}</>}
                  </div>
                </td>
                <td className="td"><SeverityBadge s={b.severity} /></td>
                <td className="td text-sm">{SOURCE_LABEL[b.source] ?? b.source}</td>
                <td className="td text-sm">{b.status.replace('_', ' ')}{b.stale && <span className="ml-1 text-xs text-muted" title="Not detected in the latest scan">(not in latest scan)</span>}</td>
                <td className="td text-sm text-muted">{timeAgo(b.last_seen_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {bugs.error && <p className="p-4 text-sm text-bad">{bugs.error.message}</p>}
      </div>
    </div>
  )
}

function BugView({ canEdit }: { canEdit: boolean }) {
  const { id } = useParams()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['bug', id], queryFn: () => api<BugDetail>(`/bugs/${id}`) })
  const refresh = () => { qc.invalidateQueries({ queryKey: ['bug', id] }); qc.invalidateQueries({ queryKey: ['bugs'] }) }
  const patch = useMutation({ mutationFn: (body: object) => api(`/bugs/${id}`, { method: 'PATCH', body }), onSuccess: refresh })
  const dup = useMutation({ mutationFn: (of: number) => api(`/bugs/${id}/mark-duplicate`, { method: 'POST', body: { of } }), onSuccess: refresh })
  const issue = useMutation({ mutationFn: () => api(`/bugs/${id}/github-issue`, { method: 'POST' }), onSuccess: refresh })
  const navigate = useNavigate()
  const runs = useQuery({ queryKey: ['runs', 'bug', id], queryFn: () => api<RunSummary[]>(`/runs?bug_id=${id}`) })
  const investigate = useMutation({ mutationFn: () => api<RunSummary>(`/bugs/${id}/investigate`, { method: 'POST' }),
    onSuccess: (r) => navigate(`/runs/${r.id}`) })
  const [showAll, setShowAll] = useState(false)
  const b = q.data
  if (q.error) return <p className="text-bad">{q.error.message}</p>
  if (!b) return <p className="text-muted">Loading…</p>
  const err = patch.error || dup.error || issue.error || investigate.error
  const active = runs.data?.find((r) => ['queued', 'running', 'waiting_approval'].includes(r.status))

  return (
    <div className="space-y-6">
      <div className="text-sm"><Link to="/bugs" className="text-muted hover:text-fg">← Bugs</Link></div>
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted">
          <SeverityBadge s={b.severity} /> <span>{SOURCE_LABEL[b.source] ?? b.source}</span> · <span>{b.repository}</span> · <span>#{b.id}</span>
          {b.github_issue_url && <a className="text-accent hover:underline" href={b.github_issue_url} target="_blank" rel="noreferrer">GitHub #{b.github_issue_number}</a>}
        </div>
        <h1 className="text-xl font-semibold tracking-tight">{b.title}</h1>
        <p className="text-sm text-muted">Seen {b.occurrences}× · first {timeAgo(b.created_at)} · last {timeAgo(b.last_seen_at)}
          {b.stale && ' · not detected in the latest scan (may be fixed; not verified)'}</p>
      </header>

      {canEdit && (
        <div className="card flex flex-wrap items-end gap-3 p-4">
          <div><label className="label" htmlFor="st">Status</label>
            <select id="st" className="input w-auto" value={b.status} disabled={b.status === 'duplicate'} onChange={(e) => patch.mutate({ status: e.target.value })}>
              {(b.status === 'duplicate' ? ['duplicate'] : STATUSES).map((s) => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select></div>
          <div><label className="label" htmlFor="sv">Severity</label>
            <select id="sv" className="input w-auto" value={b.severity} onChange={(e) => patch.mutate({ severity: e.target.value })}>
              {['critical', 'high', 'medium', 'low', 'info'].map((s) => <option key={s}>{s}</option>)}
            </select></div>
          {active ? <Link to={`/runs/${active.id}`} className="btn-primary">View active run #{active.id}</Link> :
            <button className="btn-primary" disabled={investigate.isPending || ['duplicate', 'wont_fix'].includes(b.status)}
              onClick={() => investigate.mutate()}>Investigate with AI</button>}
          {!b.github_issue_number && b.source !== 'github_issue' && (
            <button className="btn-ghost" disabled={issue.isPending} onClick={() => issue.mutate()}>Open GitHub issue</button>
          )}
          {err && <p role="alert" className="w-full text-sm text-bad">{err.message}</p>}
        </div>
      )}

      {runs.data && runs.data.length > 0 && (
        <section><h2 className="label">Debugging runs</h2>
          <ul className="card divide-y divide-line">{runs.data.map((r) => (
            <li key={r.id}><Link to={`/runs/${r.id}`} className="flex gap-3 px-4 py-2 text-sm hover:bg-bg">
              <span>#{r.id}</span><span className={RUN_STATUS_COLOR[r.status]}>{r.status.replace('_', ' ')}</span>
              <span className="text-muted">validation: {r.validation_verdict ?? '–'}</span><span className="ml-auto text-xs text-muted">{timeAgo(r.created_at)}</span>
            </Link></li>))}
          </ul></section>
      )}

      {b.description && <section><h2 className="label">Description</h2><p className="whitespace-pre-wrap text-sm">{b.description}</p></section>}

      {b.affected_files.length > 0 && (
        <section><h2 className="label">Affected files</h2>
          <ul className="space-y-1 font-mono text-sm">{b.affected_files.map((f, i) => (
            <li key={i}>{f.path}{f.line_start ? `:${f.line_start}${f.line_end && f.line_end !== f.line_start ? `-${f.line_end}` : ''}` : ''}</li>))}
          </ul></section>
      )}

      <section>
        <h2 className="label">Evidence <span className="normal-case text-muted">(secrets redacted)</span></h2>
        <div className="space-y-3">
          {(showAll ? b.evidence : b.evidence.slice(0, 2)).map((e) => (
            <div key={e.id} className="card">
              <div className="flex flex-wrap gap-2 border-b border-line px-3 py-2 text-xs text-muted">
                <span>{e.kind.replace('_', ' ')}</span>
                {e.scan_run_id && <Link to={`/scans/${e.scan_run_id}`} className="text-accent hover:underline">scan #{e.scan_run_id}</Link>}
                {e.commit_sha && <span className="font-mono">{e.commit_sha.slice(0, 12)}</span>}
                {e.file_path && <span className="font-mono">{e.file_path}{e.line_start ? `:${e.line_start}` : ''}</span>}
                <span>{timeAgo(e.created_at)}</span>
              </div>
              <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed">{e.content}</pre>
            </div>
          ))}
          {b.evidence.length > 2 && <button className="btn-ghost" onClick={() => setShowAll(!showAll)}>{showAll ? 'Show less' : `Show all ${b.evidence.length}`}</button>}
        </div>
      </section>

      {b.similar.length > 0 && (
        <section>
          <h2 className="label">Possible duplicates</h2>
          <ul className="card divide-y divide-line">
            {b.similar.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
                <Link to={`/bugs/${s.id}`} className="min-w-0 flex-1 hover:text-accent">#{s.id} {s.title}</Link>
                <span className="text-xs text-muted">{Math.round(s.similarity * 100)}% similar · {s.status}</span>
                {canEdit && b.status !== 'duplicate' && <button className="btn-ghost" onClick={() => dup.mutate(s.id)}>Mark this bug as duplicate</button>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {b.duplicate_of && <p className="text-sm">Duplicate of <Link to={`/bugs/${b.duplicate_of}`} className="text-accent">#{b.duplicate_of}</Link></p>}

      {Object.keys(b.data).length > 0 && (
        <details className="text-sm"><summary className="cursor-pointer text-muted">Technical details</summary>
          <pre className="card mt-2 overflow-x-auto p-3 font-mono text-xs">{JSON.stringify(b.data, null, 2)}</pre></details>
      )}
    </div>
  )
}
