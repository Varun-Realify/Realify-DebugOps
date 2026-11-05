import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, timeAgo } from '../api'

interface Status {
  configured: boolean; auth: string; region: string; endpoint: string; enabled: boolean; connected: boolean
  last_check: { ok?: boolean; tools?: string[]; missing_tools?: string[]; error?: string | null; checked_at?: string }; checked_at: string | null
}

/** Admin: AWS DevOps Agent connectivity (live check) and the switch for the production-incident workflow. */
export default function Integrations() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['integration', 'aws-devops-agent'], queryFn: () => api<Status>('/integrations/aws-devops-agent') })
  const set = (d: Status) => qc.setQueryData(['integration', 'aws-devops-agent'], d)
  const check = useMutation({ mutationFn: () => api<Status>('/integrations/aws-devops-agent/check', { method: 'POST' }), onSuccess: set })
  const toggle = useMutation({ mutationFn: (enabled: boolean) => api<Status>('/integrations/aws-devops-agent', { method: 'PATCH', body: { enabled } }), onSuccess: set })
  const d = q.data
  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Integrations</h1>
      <section className="card space-y-3 p-4 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-medium">AWS DevOps Agent</h2>
          {d && <span className={`rounded-full px-2 py-0.5 text-xs ${d.connected ? 'bg-ok/15 text-ok' : 'bg-line text-muted'}`}>{d.connected ? 'connected (last check passed)' : 'not verified'}</span>}
          {d?.enabled && <span className="rounded-full bg-accent/15 px-2 py-0.5 text-xs text-accent">incident workflow on</span>}
        </div>
        <p className="text-muted">Investigates production incidents (CloudWatch logs and metrics, traces, deployment history) over MCP before Claude Code fixes the code.
          Used only for tasks classified as production incidents.</p>
        {d && (
          <dl className="grid gap-2 sm:grid-cols-2">
            <div><dt className="text-xs text-muted">Endpoint</dt><dd className="break-all font-mono text-xs">{d.endpoint}</dd></div>
            <div><dt className="text-xs text-muted">Authentication</dt><dd className="text-xs">{d.auth} · {d.configured ? 'token configured' : <span className="text-warn">DEVOPS_AGENT_TOKEN not set</span>}</dd></div>
            <div><dt className="text-xs text-muted">Last check</dt><dd className="text-xs">{d.checked_at ? `${timeAgo(d.checked_at)} · ${d.last_check.ok ? 'OK' : 'failed'}` : 'never'}</dd></div>
            <div><dt className="text-xs text-muted">Tools</dt><dd className="text-xs">{d.last_check.tools?.length ?? 0} discovered{d.last_check.missing_tools?.length ? `, missing: ${d.last_check.missing_tools.join(', ')}` : ''}</dd></div>
          </dl>
        )}
        {d?.last_check.error && <p className="text-xs text-bad">{d.last_check.error}</p>}
        <div className="flex flex-wrap gap-2">
          <button className="btn-ghost" disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? 'Checking…' : 'Run connectivity check'}</button>
          {d && <button className={d.enabled ? 'btn-ghost' : 'btn-primary'} disabled={toggle.isPending || (!d.enabled && !d.connected)}
            onClick={() => toggle.mutate(!d.enabled)}>{d.enabled ? 'Disable incident workflow' : 'Enable incident workflow'}</button>}
        </div>
        {(check.error || toggle.error) && <p role="alert" className="text-xs text-bad">{(check.error || toggle.error)!.message}</p>}
        <p className="text-xs text-muted">Enabling requires a passing live check. A failing check disables it automatically. Setup: docs/AWS_DEVOPS_AGENT.md.</p>
      </section>
    </div>
  )
}
