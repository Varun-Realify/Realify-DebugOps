import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export interface LiveEvent {
  kind: 'event' | 'run' | 'scan' | 'ready' | 'ping'
  id: number
  run_id?: number | null
  scan_id?: string | null
  level?: string
  type?: string
  source?: string
  message?: string
  ts?: string
  status?: string
  current_node?: string
  bug_id?: number
}

/** Live updates over WebSocket. Reconnects with backoff and replays from the last seen event id, so nothing is lost.
 *  Run/scan status changes invalidate the matching React Query caches; activity events are returned for display. */
export function useEventStream(opts: { runId?: number; keep?: number } = {}) {
  const qc = useQueryClient()
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [connected, setConnected] = useState(false)
  const lastId = useRef(0)
  const keep = opts.keep ?? 300

  useEffect(() => {
    let ws: WebSocket | null = null
    let stopped = false
    let delay = 1000
    let timer: ReturnType<typeof setTimeout> | undefined

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const qs = new URLSearchParams({ after: String(lastId.current) })
      if (opts.runId) qs.set('run_id', String(opts.runId))
      ws = new WebSocket(`${proto}://${location.host}/api/v1/ws?${qs}`)
      ws.onopen = () => { setConnected(true); delay = 1000 }
      ws.onmessage = (m) => {
        const e: LiveEvent = JSON.parse(m.data)
        if (e.kind === 'event') {
          if (e.id <= lastId.current) return
          lastId.current = e.id
          setEvents((prev) => [...prev.slice(-(keep - 1)), e])
          qc.invalidateQueries({ queryKey: ['dashboard'] })
          if (e.run_id) qc.invalidateQueries({ queryKey: ['run', String(e.run_id)] })
        } else if (e.kind === 'run') {
          qc.invalidateQueries({ queryKey: ['runs'] })
          qc.invalidateQueries({ queryKey: ['run', String(e.id)] })
          qc.invalidateQueries({ queryKey: ['dashboard'] })
          if (e.bug_id) qc.invalidateQueries({ queryKey: ['bug', String(e.bug_id)] })
        } else if (e.kind === 'scan') {
          qc.invalidateQueries({ queryKey: ['scans'] })
          qc.invalidateQueries({ queryKey: ['scan', String(e.id)] })
          qc.invalidateQueries({ queryKey: ['dashboard'] })
          qc.invalidateQueries({ queryKey: ['bugs'] })
        }
      }
      ws.onclose = () => {
        setConnected(false)
        if (!stopped) { timer = setTimeout(connect, delay); delay = Math.min(delay * 2, 30000) }
      }
    }
    connect()
    return () => { stopped = true; clearTimeout(timer); ws?.close() }
  }, [opts.runId, keep, qc])

  return { events, connected }
}
