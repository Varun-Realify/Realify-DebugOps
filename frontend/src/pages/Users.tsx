import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, timeAgo, type Role, type User } from '../api'

const ROLES: Role[] = ['admin', 'developer', 'reviewer']
const EMPTY = { email: '', name: '', role: 'developer' as Role, password: '' }

export default function Users({ me }: { me: User }) {
  const qc = useQueryClient()
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<User[]>('/users') })
  const [form, setForm] = useState(EMPTY)
  const [resetFor, setResetFor] = useState<User | null>(null)
  const [newPassword, setNewPassword] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ['users'] })

  const create = useMutation({
    mutationFn: () => api<User>('/users', { method: 'POST', body: form }),
    onSuccess: () => { setForm(EMPTY); refresh() },
  })
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<User> & { password?: string } }) =>
      api<User>(`/users/${id}`, { method: 'PATCH', body }),
    onSuccess: () => { refresh(); closeReset() },
  })

  const openReset = (u: User) => { setResetFor(u); setNewPassword(''); update.reset(); dialog.current?.showModal() }
  const closeReset = () => { dialog.current?.close(); setResetFor(null) }

  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate() }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold tracking-tight">Users</h1>

      <form onSubmit={submit} className="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-5 lg:items-end">
        <div>
          <label className="label" htmlFor="u-email">Email</label>
          <input id="u-email" className="input" type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="u-name">Name</label>
          <input id="u-name" className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="u-role">Role</label>
          <select id="u-role" className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            {ROLES.map((r) => <option key={r}>{r}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="u-pass">Initial password</label>
          <input id="u-pass" className="input" type="password" autoComplete="new-password" minLength={12} required
            value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        </div>
        <button className="btn-primary" disabled={create.isPending}>Add user</button>
        {create.error && <p role="alert" className="text-sm text-bad sm:col-span-2 lg:col-span-5">{create.error.message}</p>}
      </form>

      {update.error && !resetFor && <p role="alert" className="text-sm text-bad">{update.error.message}</p>}

      <div className="card overflow-x-auto">
        <table className="w-full min-w-[640px]">
          <thead>
            <tr><th className="th">User</th><th className="th">Role</th><th className="th">Status</th><th className="th">Last login</th><th className="th" /></tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id} className={u.is_active ? '' : 'opacity-50'}>
                <td className="td">
                  <div className="font-medium">{u.name || '—'}{u.id === me.id && <span className="ml-2 text-xs text-muted">(you)</span>}</div>
                  <div className="text-xs text-muted">{u.email}</div>
                </td>
                <td className="td">
                  <select aria-label={`Role for ${u.email}`} className="input w-auto py-1" value={u.role}
                    onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value as Role } })}>
                    {ROLES.map((r) => <option key={r}>{r}</option>)}
                  </select>
                </td>
                <td className="td">{u.is_active ? <span className="text-ok">Active</span> : <span className="text-muted">Disabled</span>}</td>
                <td className="td text-muted">{timeAgo(u.last_login_at)}</td>
                <td className="td whitespace-nowrap text-right">
                  <button className="btn-ghost mr-2" onClick={() => openReset(u)}>Reset password</button>
                  <button className="btn-ghost" onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                    {u.is_active ? 'Disable' : 'Enable'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.error && <p className="p-4 text-sm text-bad">{users.error.message}</p>}
      </div>

      <dialog ref={dialog} onClose={() => setResetFor(null)} className="card m-auto w-full max-w-sm p-6 text-fg backdrop:bg-black/50">
        <form onSubmit={(e) => { e.preventDefault(); if (resetFor) update.mutate({ id: resetFor.id, body: { password: newPassword } }) }}>
          <h2 className="mb-1 font-semibold">Reset password</h2>
          <p className="mb-4 text-sm text-muted">{resetFor?.email} will be signed out everywhere and unlocked.</p>
          <label className="label" htmlFor="reset-pass">New password</label>
          <input id="reset-pass" className="input mb-4" type="password" autoComplete="new-password" minLength={12} required
            value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
          {update.error && resetFor && <p role="alert" className="mb-4 text-sm text-bad">{update.error.message}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={closeReset}>Cancel</button>
            <button className="btn-primary" disabled={update.isPending}>Reset</button>
          </div>
        </form>
      </dialog>
    </div>
  )
}
