import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  GitPullRequest,
  CircleDot,
  History,
  Brain,
  Sparkles,
  Users,
  Network,
  Settings,
  LogOut,
} from 'lucide-react'
import { useUser } from '@/hooks/useUser'
import { post } from '@/lib/api'
import { cn } from '@/lib/cn'

const NAV = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/prs', label: 'Pull Requests', icon: GitPullRequest },
  { to: '/issues', label: 'Issues', icon: CircleDot },
  { to: '/runs', label: 'Runs', icon: History },
  { to: '/memory', label: 'Memory', icon: Brain },
  { to: '/skills', label: 'Skills', icon: Sparkles },
  { to: '/agents', label: 'Agents', icon: Users },
  { to: '/graph', label: 'Graph', icon: Network },
  { to: '/config', label: 'Config', icon: Settings },
]

export default function Layout() {
  const { user, refresh } = useUser()
  const navigate = useNavigate()

  async function handleLogout() {
    try {
      await post('/api/auth/logout')
    } catch {
      // ignore
    }
    await refresh(undefined, { revalidate: false })
    navigate('/login')
  }

  return (
    <div className="flex min-h-screen">
      <aside className="w-60 shrink-0 border-r border-[var(--color-border)] bg-[var(--color-card)] flex flex-col">
        <div className="px-5 py-4 border-b border-[var(--color-border)]">
          <h1 className="text-lg font-semibold tracking-tight">Caretaker</h1>
          <p className="text-xs text-[var(--color-muted-foreground)]">Admin Dashboard</p>
        </div>
        <nav className="flex-1 overflow-y-auto p-3 space-y-1">
          {NAV.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors',
                    isActive
                      ? 'bg-[var(--color-primary)] text-[var(--color-primary-foreground)]'
                      : 'text-[var(--color-foreground)] hover:bg-[var(--color-muted)]',
                  )
                }
              >
                <Icon className="h-4 w-4" />
                {item.label}
              </NavLink>
            )
          })}
        </nav>
        {user && (
          <div className="border-t border-[var(--color-border)] p-3">
            <div className="flex items-center gap-2 px-2 py-1.5">
              {user.picture ? (
                <img
                  src={user.picture}
                  alt=""
                  className="h-7 w-7 rounded-full"
                />
              ) : (
                <div className="h-7 w-7 rounded-full bg-[var(--color-muted)] grid place-items-center text-xs">
                  {(user.name || user.email || '?').charAt(0).toUpperCase()}
                </div>
              )}
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium truncate">{user.name || user.email}</p>
                {user.email && (
                  <p className="text-[10px] text-[var(--color-muted-foreground)] truncate">
                    {user.email}
                  </p>
                )}
              </div>
            </div>
            <button
              onClick={handleLogout}
              className="mt-1 w-full flex items-center gap-2 px-3 py-1.5 rounded-md text-xs text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)] hover:text-[var(--color-foreground)]"
            >
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </button>
          </div>
        )}
      </aside>
      <main className="flex-1 min-w-0">
        <Outlet />
      </main>
    </div>
  )
}
