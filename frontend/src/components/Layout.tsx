import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard,
  GitPullRequest,
  CircleDot,
  History,
  Brain,
  Sparkles,
  Users,
  Network,
  Boxes,
  Settings,
  LogOut,
  ChevronRight,
  Activity,
} from 'lucide-react'
import { useUser } from '@/hooks/useUser'
import { post } from '@/lib/api'
import { cn } from '@/lib/cn'
import ThemeToggle from '@/components/ThemeToggle'

const NAV = [
  { to: '/', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/prs', label: 'Pull Requests', icon: GitPullRequest },
  { to: '/issues', label: 'Issues', icon: CircleDot },
  { to: '/runs', label: 'Runs', icon: History },
  { to: '/streams', label: 'Live Streams', icon: Activity },
  { to: '/memory', label: 'Memory', icon: Brain },
  { to: '/skills', label: 'Skills', icon: Sparkles },
  { to: '/agents', label: 'Agents', icon: Users },
  { to: '/graph', label: 'Graph', icon: Network },
  { to: '/fleet', label: 'Fleet', icon: Boxes },
  { to: '/config', label: 'Config', icon: Settings },
]

function crumbFor(pathname: string): string {
  if (pathname === '/') return 'Overview'
  const segment = pathname.split('/').filter(Boolean)[0] ?? ''
  const match = NAV.find((n) => n.to.replace('/', '') === segment)
  if (match) return match.label
  return segment.charAt(0).toUpperCase() + segment.slice(1)
}

export default function Layout() {
  const { user, refresh } = useUser()
  const navigate = useNavigate()
  const location = useLocation()
  const crumb = crumbFor(location.pathname)

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
      <aside
        className={cn(
          'w-60 shrink-0 flex flex-col sticky top-0 h-screen z-20',
          'glass border-r border-[var(--color-border)]',
        )}
      >
        <div className="px-5 py-4 border-b border-[var(--color-border)]">
          <div className="flex items-center gap-2">
            <div
              aria-hidden
              className="h-7 w-7 rounded-md grid place-items-center text-[var(--color-primary-foreground)]"
              style={{
                background:
                  'linear-gradient(135deg, var(--color-primary), var(--color-accent))',
                boxShadow: 'var(--shadow-glow)',
              }}
            >
              <Activity className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <h1 className="text-sm font-semibold tracking-tight leading-tight">
                Caretaker
              </h1>
              <p className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted-foreground)]">
                Orchestrator
              </p>
            </div>
          </div>
        </div>
        <nav className="flex-1 overflow-y-auto p-3 space-y-0.5">
          {NAV.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  cn(
                    'group relative flex items-center gap-2.5 px-3 py-2 rounded-[var(--radius-sm)] text-sm',
                    'transition-colors duration-[var(--motion-fast)]',
                    isActive
                      ? 'text-[var(--color-foreground)] bg-[var(--color-primary-soft)]'
                      : 'text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] hover:bg-[var(--color-muted)]',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <span
                      aria-hidden
                      className={cn(
                        'absolute left-0 top-1/2 -translate-y-1/2 h-5 w-[2px] rounded-r-full transition-opacity',
                        isActive ? 'opacity-100' : 'opacity-0',
                      )}
                      style={{ background: 'var(--color-primary)' }}
                    />
                    <Icon
                      className={cn(
                        'h-4 w-4 shrink-0',
                        isActive && 'text-[var(--color-primary)]',
                      )}
                    />
                    <span>{item.label}</span>
                  </>
                )}
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
                  className="h-7 w-7 rounded-full ring-1 ring-[var(--color-border)]"
                />
              ) : (
                <div className="h-7 w-7 rounded-full bg-[var(--color-muted)] grid place-items-center text-xs">
                  {(user.name || user.email || '?').charAt(0).toUpperCase()}
                </div>
              )}
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium truncate">
                  {user.name || user.email}
                </p>
                {user.email && (
                  <p className="text-[10px] text-[var(--color-muted-foreground)] truncate">
                    {user.email}
                  </p>
                )}
              </div>
            </div>
            <button
              onClick={handleLogout}
              className={cn(
                'mt-1 w-full flex items-center gap-2 px-3 py-1.5 rounded-[var(--radius-sm)] text-xs',
                'text-[var(--color-muted-foreground)] hover:bg-[var(--color-muted)] hover:text-[var(--color-foreground)]',
                'transition-colors',
              )}
            >
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </button>
          </div>
        )}
      </aside>
      <main className="flex-1 min-w-0 flex flex-col">
        <header
          className={cn(
            'sticky top-0 z-10 glass border-b border-[var(--color-border)]',
            'flex items-center justify-between px-6 h-14',
          )}
        >
          <nav
            aria-label="Breadcrumb"
            className="flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]"
          >
            <span>Caretaker</span>
            <ChevronRight className="h-3.5 w-3.5" aria-hidden />
            <span className="text-[var(--color-foreground)] font-medium">
              {crumb}
            </span>
          </nav>
          <div className="flex items-center gap-2">
            <ThemeToggle />
          </div>
        </header>
        <div className="flex-1 min-w-0">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
