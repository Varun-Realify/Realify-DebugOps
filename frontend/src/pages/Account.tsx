import { useState, type FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api'
import { useMe } from '../App'

export default function Account({ onSignOut }: { onSignOut: () => void }) {
  const { data: me } = useMe()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const change = useMutation({
    mutationFn: () => api('/auth/change-password', { method: 'POST', body: { current_password: current, new_password: next } }),
    onSuccess: () => { setCurrent(''); setNext(''); setConfirm('') },
  })
  const mismatch = confirm.length > 0 && next !== confirm

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!mismatch) change.mutate()
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Account</h1>
      <div className="card p-4 text-sm">
        <div className="font-medium">{me?.name || me?.email}</div>
        <div className="text-muted">{me?.email} · <span className="capitalize">{me?.role}</span></div>
        <button className="btn-ghost mt-4 md:hidden" onClick={onSignOut}>Sign out</button>
      </div>
      <form onSubmit={submit} className="card max-w-md space-y-4 p-4">
        <h2 className="font-medium">Change password</h2>
        <div>
          <label className="label" htmlFor="cur">Current password</label>
          <input id="cur" className="input" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="new">New password (min 12 characters)</label>
          <input id="new" className="input" type="password" autoComplete="new-password" minLength={12} required value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="confirm">Confirm new password</label>
          <input id="confirm" className="input" type="password" autoComplete="new-password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </div>
        {mismatch && <p className="text-sm text-bad">Passwords do not match.</p>}
        {change.error && <p role="alert" className="text-sm text-bad">{change.error.message}</p>}
        {change.isSuccess && <p role="status" className="text-sm text-ok">Password changed. Other sessions were signed out.</p>}
        <button className="btn-primary" disabled={change.isPending || mismatch}>Change password</button>
      </form>
    </div>
  )
}
