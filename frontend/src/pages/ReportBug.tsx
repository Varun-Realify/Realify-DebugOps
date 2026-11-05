import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Repository, type Severity } from '../api'

const MAX_FILE = 1_000_000

interface Result { bug_id: number; run_id?: number | null; similar: { id: number; title: string; similarity: number }[];
  github_issue_url?: string; github_issue_error?: string; run_error?: string }

/** Developer bug upload: description, steps, stack trace, affected files and log files (read as text in the browser). */
export default function ReportBug() {
  const navigate = useNavigate()
  const repos = useQuery({ queryKey: ['repos'], queryFn: () => api<Repository[]>('/repositories') })
  const usable = (repos.data ?? []).filter((r) => r.enabled && r.accessible && r.installation_active)
  const [f, setF] = useState({ repo: '', title: '', description: '', steps_to_reproduce: '', stack_trace: '', severity: 'medium' as Severity,
    affected_files: '', open_github_issue: false, investigate: true })
  const [files, setFiles] = useState<{ name: string; content: string }[]>([])
  const [fileError, setFileError] = useState('')
  const submit = useMutation({
    mutationFn: () => api<Result>(`/repositories/${f.repo || usable[0]?.id}/bugs`, { method: 'POST', body: {
      title: f.title, description: f.description, steps_to_reproduce: f.steps_to_reproduce, stack_trace: f.stack_trace,
      severity: f.severity, affected_files: f.affected_files.split(/[\n,]/).map((x) => x.trim()).filter(Boolean),
      attachments: files, open_github_issue: f.open_github_issue, investigate: f.investigate } }),
  })

  const onFiles = async (list: FileList | null) => {
    setFileError('')
    const picked = Array.from(list ?? []).slice(0, 5)
    const tooBig = picked.find((x) => x.size > MAX_FILE)
    if (tooBig) { setFileError(`${tooBig.name} is larger than 1 MB; attach an excerpt instead.`); return }
    setFiles(await Promise.all(picked.map(async (x) => ({ name: x.name, content: await x.text() }))))
  }
  const onSubmit = (e: FormEvent) => { e.preventDefault(); submit.mutate() }
  const r = submit.data

  if (r) return (
    <div className="max-w-2xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Bug reported</h1>
      <p className="text-sm">Bug <Link className="text-accent" to={`/bugs/${r.bug_id}`}>#{r.bug_id}</Link> was created. Secrets in the text and logs were redacted before storage.</p>
      {r.run_id && <p className="text-sm">AI investigation started: <Link className="text-accent" to={`/runs/${r.run_id}`}>run #{r.run_id}</Link>.</p>}
      {r.run_error && <p className="text-sm text-warn">Investigation not started: {r.run_error}</p>}
      {r.github_issue_url && <p className="text-sm">GitHub issue: <a className="text-accent" href={r.github_issue_url} target="_blank" rel="noreferrer">{r.github_issue_url}</a></p>}
      {r.github_issue_error && <p className="text-sm text-warn">GitHub issue not created: {r.github_issue_error}</p>}
      {r.similar.length > 0 && (
        <div className="card border-warn p-4 text-sm"><div className="font-medium">Possible duplicates</div>
          <ul className="mt-1">{r.similar.map((s) => <li key={s.id}><Link className="text-accent" to={`/bugs/${s.id}`}>#{s.id} {s.title}</Link> ({Math.round(s.similarity * 100)}% similar)</li>)}</ul>
        </div>)}
      <button className="btn-ghost" onClick={() => navigate(r.run_id ? `/runs/${r.run_id}` : `/bugs/${r.bug_id}`)}>Open</button>
    </div>
  )

  return (
    <form onSubmit={onSubmit} className="max-w-3xl space-y-4">
      <div className="text-sm"><Link to="/bugs" className="text-muted hover:text-fg">← Bugs</Link></div>
      <h1 className="text-xl font-semibold tracking-tight">Report a bug</h1>
      {repos.data && usable.length === 0 && <p className="text-sm text-warn">No enabled repositories. An admin must connect and enable one first.</p>}
      <div className="grid gap-4 sm:grid-cols-3">
        <label className="block text-sm sm:col-span-2"><span className="label">Repository</span>
          <select className="input" value={f.repo || String(usable[0]?.id ?? '')} onChange={(e) => setF({ ...f, repo: e.target.value })} required>
            {usable.map((x) => <option key={x.id} value={x.id}>{x.full_name}</option>)}
          </select></label>
        <label className="block text-sm"><span className="label">Severity</span>
          <select className="input" value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value as Severity })}>
            {['critical', 'high', 'medium', 'low', 'info'].map((x) => <option key={x}>{x}</option>)}
          </select></label>
      </div>
      <label className="block text-sm"><span className="label">Title</span>
        <input className="input" required minLength={3} maxLength={300} placeholder="Login API returns 500" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></label>
      <label className="block text-sm"><span className="label">What happens / what should happen</span>
        <textarea className="input min-h-28" maxLength={20000} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></label>
      <label className="block text-sm"><span className="label">Steps to reproduce</span>
        <textarea className="input min-h-20" maxLength={10000} value={f.steps_to_reproduce} onChange={(e) => setF({ ...f, steps_to_reproduce: e.target.value })} /></label>
      <label className="block text-sm"><span className="label">Stack trace / error output</span>
        <textarea className="input min-h-28 font-mono text-xs" maxLength={50000} value={f.stack_trace} onChange={(e) => setF({ ...f, stack_trace: e.target.value })} /></label>
      <label className="block text-sm"><span className="label">Suspected files (optional, one per line)</span>
        <textarea className="input min-h-16 font-mono text-xs" placeholder="app/auth.py" value={f.affected_files} onChange={(e) => setF({ ...f, affected_files: e.target.value })} /></label>
      <label className="block text-sm"><span className="label">Log files (text, up to 5 × 1 MB)</span>
        <input type="file" multiple accept=".log,.txt,.json,.out,.err,text/*" className="block text-sm" onChange={(e) => onFiles(e.target.files)} />
        {files.length > 0 && <span className="text-xs text-muted">{files.map((x) => x.name).join(', ')}</span>}
        {fileError && <span className="block text-xs text-bad">{fileError}</span>}</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.investigate} onChange={(e) => setF({ ...f, investigate: e.target.checked })} />Start AI investigation now</label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={f.open_github_issue} onChange={(e) => setF({ ...f, open_github_issue: e.target.checked })} />Also open a GitHub issue</label>
      {submit.error && <p role="alert" className="text-sm text-bad">{submit.error.message}</p>}
      <button className="btn-primary" disabled={submit.isPending || usable.length === 0 || !!fileError}>{submit.isPending ? 'Submitting…' : 'Report bug'}</button>
    </form>
  )
}
