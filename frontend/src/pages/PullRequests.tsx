import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, timeAgo } from '../api'
import { useEventStream } from '../realtime/useEventStream'

interface PRItem {
  kind: 'pull_request' | 'pending_approval'
  id: number | null; run_id: number; task_id: string; task_title: string; repository: string
  number: number | null; url: string | null; state: string; head_branch: string | null
  ci_status: string | null; checks: { name: string; conclusion: string | null; status: string }[]
  review_state: string | null; awaiting_developer: boolean; validation: string | null
  files_changed: string[]; test_summary: { passed?: number; failed?: number }; created_at: string; merged_at: string | null
  post_merge_ci_status: string | null; deployments: { environment: string; state: string }[]; health: string | null
}

const tone = (v?: string | null) => !v ? 'text-muted' : ['success', 'approved', 'verified', 'merged', 'healthy'].includes(v) ? 'text-ok'
  : ['failure', 'changes_requested', 'failed', 'unhealthy', 'closed'].includes(v) ? 'text-bad' : 'text-warn'

/** Proposed fixes awaiting developer approval (and their journey after merge). */
export default function PullRequests() {
  const q = useQuery({ queryKey: ['pull-requests'], queryFn: () => api<PRItem[]>('/pull-requests') })
  useEventStream({ keep: 1 })  // run status changes refresh this list
  const [view, setView] = useState<'awaiting' | 'all'>('awaiting')
  const items = (q.data ?? []).filter((p) => view === 'all' || p.awaiting_developer)
  const waiting = (q.data ?? []).filter((p) => p.awaiting_developer).length

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Pull requests</h1>
        <div className="flex gap-1" role="tablist">
          <button role="tab" aria-selected={view === 'awaiting'} className={view === 'awaiting' ? 'btn-primary' : 'btn-ghost'} onClick={() => setView('awaiting')}>Awaiting developer ({waiting})</button>
          <button role="tab" aria-selected={view === 'all'} className={view === 'all' ? 'btn-primary' : 'btn-ghost'} onClick={() => setView('all')}>All</button>
        </div>
      </div>
      <p className="text-sm text-muted">Fixes the AI proposed after they passed DebugOps validation. Developers review, approve and merge them in GitHub; DebugOps never merges.</p>
      {q.error && <p className="text-sm text-bad">{q.error.message}</p>}
      {items.length === 0 && !q.isLoading && <div className="card p-8 text-center text-sm text-muted">{view === 'awaiting' ? 'Nothing is waiting for review.' : 'No pull requests yet.'}</div>}
      <div className="space-y-3">
        {items.map((p) => (
          <article key={`${p.kind}-${p.run_id}`} className="card p-4">
            <div className="flex flex-wrap items-start gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                  <span className="font-mono">{p.task_id}</span><span>· {p.repository}</span>
                  {p.head_branch && <span className="font-mono">· {p.head_branch}</span>}<span>· {timeAgo(p.created_at)}</span>
                </div>
                <Link to={`/runs/${p.run_id}`} className="mt-0.5 block font-medium hover:text-accent">{p.task_title}</Link>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                  <span>Validation: <b className={tone(p.validation)}>{p.validation ?? '–'}</b></span>
                  <span>Tests: <b>{p.test_summary?.passed ?? '–'} passed</b>{p.test_summary?.failed ? <b className="text-bad">, {p.test_summary.failed} failed</b> : null}</span>
                  <span>Files: <span className="font-mono">{p.files_changed.slice(0, 4).join(', ') || '–'}</span></span>
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1 text-xs">
                {p.kind === 'pending_approval'
                  ? <Link to={`/runs/${p.run_id}`} className="btn-primary">Review in DebugOps</Link>
                  : <a className="btn-primary" href={p.url!} target="_blank" rel="noreferrer">Review PR #{p.number} on GitHub</a>}
                <span className={tone(p.review_state)}>{p.kind === 'pending_approval' ? 'approval required before PR' : `review: ${p.review_state ?? 'not reviewed yet'}`}</span>
                {p.ci_status && <span className={tone(p.ci_status)}>CI: {p.ci_status}</span>}
                {p.state === 'merged' && <span className="text-ok">merged · main CI: <b className={tone(p.post_merge_ci_status)}>{p.post_merge_ci_status ?? '–'}</b></span>}
                {p.deployments.length > 0 && <span>{p.deployments.map((d) => `${d.environment}: ${d.state}`).join(' · ')}</span>}
                {p.health && <span className={tone(p.health)}>health: {p.health}</span>}
              </div>
            </div>
          </article>
        ))}
      </div>
    </div>
  )
}
