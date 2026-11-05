import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type User } from '../api'

export default function Login() {
  const qc = useQueryClient()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const login = useMutation({
    mutationFn: () => api<User>('/auth/login', { method: 'POST', body: { email, password } }),
    onSuccess: (user) => qc.setQueryData(['me'], user),
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    login.mutate()
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="card w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold tracking-tight">
          Realify <span className="text-accent">DebugOps</span>
        </h1>
        <p className="mb-6 text-sm text-muted">Sign in to continue</p>
        <label className="label" htmlFor="email">Email</label>
        <input id="email" className="input mb-4" type="email" autoComplete="username" required autoFocus
          value={email} onChange={(e) => setEmail(e.target.value)} />
        <label className="label" htmlFor="password">Password</label>
        <input id="password" className="input mb-4" type="password" autoComplete="current-password" required
          value={password} onChange={(e) => setPassword(e.target.value)} />
        {login.error && (
          <p role="alert" className="mb-4 text-sm text-bad">
            {login.error.message}. Accounts lock for 15 minutes after 5 failed attempts.
          </p>
        )}
        <button className="btn-primary w-full" disabled={login.isPending}>
          {login.isPending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
