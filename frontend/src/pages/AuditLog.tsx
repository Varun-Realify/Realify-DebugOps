import { useInfiniteQuery } from '@tanstack/react-query'
import { api, type AuditEntry } from '../api'

const PAGE = 100

export default function AuditLog() {
  const q = useInfiniteQuery({
    queryKey: ['audit'],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api<AuditEntry[]>(`/audit-logs?limit=${PAGE}${pageParam ? `&before_id=${pageParam}` : ''}`),
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].id : undefined),
  })
  const rows = q.data?.pages.flat() ?? []

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Audit log</h1>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[720px]">
          <thead>
            <tr><th className="th">Time</th><th className="th">Action</th><th className="th">Actor</th><th className="th">Target</th><th className="th">Details</th><th className="th">IP</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="td whitespace-nowrap text-muted">{new Date(r.ts).toLocaleString()}</td>
                <td className="td font-mono text-xs">{r.action}</td>
                <td className="td">{r.actor_user_id ? `user #${r.actor_user_id}` : r.actor_type}</td>
                <td className="td">{r.target_type ? `${r.target_type} #${r.target_id}` : '—'}</td>
                <td className="td max-w-xs truncate font-mono text-xs text-muted" title={JSON.stringify(r.data)}>
                  {Object.keys(r.data).length ? JSON.stringify(r.data) : ''}
                </td>
                <td className="td font-mono text-xs text-muted">{r.ip ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {q.error && <p className="p-4 text-sm text-bad">{q.error.message}</p>}
      </div>
      {q.hasNextPage && (
        <button className="btn-ghost" onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>Load more</button>
      )}
    </div>
  )
}
