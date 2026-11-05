import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { api, ApiError, type Role, type User } from './api'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import Users from './pages/Users'
import AuditLog from './pages/AuditLog'
import Account from './pages/Account'
import Repositories from './pages/Repositories'
import Bugs from './pages/Bugs'
import ReportBug from './pages/ReportBug'
import Errors from './pages/Errors'
import PullRequests from './pages/PullRequests'
import Integrations from './pages/Integrations'
import Scans from './pages/Scans'
import { lazy, Suspense } from 'react'
const Runs = lazy(() => import('./pages/Runs'))  // React Flow is heavy: load only on the Runs pages

export function useMe() {
  return useQuery<User | null>({
    queryKey: ['me'],
    queryFn: () =>
      api<User>('/auth/me').catch((e) => {
        if (e instanceof ApiError && e.status === 401) return null
        throw e
      }),
    staleTime: 60_000,
  })
}

const NAV: { to: string; label: string; roles?: Role[] }[] = [
  { to: '/', label: 'Dashboard' },
  { to: '/runs', label: 'Tasks' },
  { to: '/pull-requests', label: 'Pull requests' },
  { to: '/bugs', label: 'Bugs' },
  { to: '/scans', label: 'Scans' },
  { to: '/repositories', label: 'Repositories' },
  { to: '/users', label: 'Users', roles: ['admin'] },
  { to: '/audit', label: 'Audit log', roles: ['admin'] },
  { to: '/errors', label: 'System errors', roles: ['admin'] },
  { to: '/integrations', label: 'Integrations', roles: ['admin'] },
  { to: '/account', label: 'Account' },
]

export default function App() {
  const { data: me, isLoading, error } = useMe()
  const qc = useQueryClient()
  const logout = useMutation({
    mutationFn: () => api('/auth/logout', { method: 'POST' }),
    onSettled: () => {
      qc.clear()
      qc.setQueryData(['me'], null)
    },
  })

  if (isLoading) return <div className="p-8 text-muted">Loading…</div>
  if (error) return <div className="p-8 text-bad">Cannot reach the API: {(error as Error).message}</div>
  if (!me) return <Login />

  const allowed = (roles?: Role[]) => !roles || roles.includes(me.role)

  return (
    <div className="min-h-screen md:flex">
      <aside className="border-b border-line bg-panel md:sticky md:top-0 md:flex md:h-screen md:w-56 md:shrink-0 md:flex-col md:border-r md:border-b-0">
        <div className="flex items-center justify-between px-4 py-3 md:block md:py-5">
          <div className="font-semibold tracking-tight">
            Realify <span className="text-accent">DebugOps</span>
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-0">
          {NAV.filter((n) => allowed(n.roles)).map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === '/'}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-md px-3 py-2 text-sm ${isActive ? 'bg-bg font-medium text-fg' : 'text-muted hover:text-fg'}`
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="hidden border-t border-line p-4 text-sm md:mt-auto md:block">
          <div className="truncate font-medium">{me.name || me.email}</div>
          <div className="mb-3 text-xs capitalize text-muted">{me.role}</div>
          <button className="btn-ghost w-full" onClick={() => logout.mutate()} disabled={logout.isPending}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="mx-auto w-full min-w-0 max-w-6xl flex-1 px-4 py-6 md:px-8">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/repositories/*" element={<Repositories me={me} />} />
          {me.role !== 'reviewer' && <Route path="/bugs/new" element={<ReportBug />} />}
          <Route path="/bugs/*" element={<Bugs me={me} />} />
          <Route path="/scans/*" element={<Scans />} />
          <Route path="/pull-requests" element={<PullRequests />} />
          <Route path="/runs/*" element={<Suspense fallback={<p className="text-muted">Loading…</p>}><Runs me={me} /></Suspense>} />
          {me.role === 'admin' && <Route path="/users" element={<Users me={me} />} />}
          {me.role === 'admin' && <Route path="/audit" element={<AuditLog />} />}
          {me.role === 'admin' && <Route path="/errors" element={<Errors />} />}
          {me.role === 'admin' && <Route path="/integrations" element={<Integrations />} />}
          <Route path="/account" element={<Account onSignOut={() => logout.mutate()} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
