import { useInfiniteQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, timeAgo } from '../api'

interface Ev { id: number; ts: string; source: string; message: string; run_id: number | null; scan_id: string | null; request_id: string | null }
const PAGE = 100

/** Centralized view of errors recorded by every service (API 500s, failed jobs, scans, runs, regressions). */
export default function Errors() {
  const q = useInfiniteQuery({
    queryKey: ['errors'], initialPageParam: 0,
    queryFn: ({ pageParam }) => api<Ev[]>(`/activity?level=error&limit=${PAGE}${pageParam ? `&before_id=${pageParam}` : ''}`),
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].id : undefined),
  })
  const rows = q.data?.pages.flat() ?? []
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">System errors</h1>
      <p className="text-sm text-muted">Errors from the API, workers, scans and debugging runs. Full logs (JSON, with request ids) are in the
        centralized log store; optional Sentry receives exceptions with stack traces.</p>
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[720px]">
          <thead><tr><th className="th">When</th><th className="th">Source</th><th className="th">Error</th><th className="th">Context</th></tr></thead>
          <tbody>
            {rows.length === 0 && !q.isLoading && <tr><td className="td py-8 text-center text-muted" colSpan={4}>No errors recorded.</td></tr>}
            {rows.map((e) => (
              <tr key={e.id} className="align-top">
                <td className="td whitespace-nowrap text-muted" title={e.ts}>{timeAgo(e.ts)}</td>
                <td className="td font-mono text-xs">{e.source}</td>
                <td className="td text-sm break-words">{e.message}</td>
                <td className="td whitespace-nowrap text-xs">
                  {e.run_id && <Link className="text-accent" to={`/runs/${e.run_id}`}>run #{e.run_id}</Link>}
                  {e.scan_id && <Link className="text-accent" to={`/scans/${e.scan_id}`}>scan #{e.scan_id}</Link>}
                  {e.request_id && <span className="font-mono text-muted">req {e.request_id.slice(0, 12)}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {q.error && <p className="p-4 text-sm text-bad">{q.error.message}</p>}
      </div>
      {q.hasNextPage && <button className="btn-ghost" onClick={() => q.fetchNextPage()}>Load more</button>}
    </div>
  )
}
