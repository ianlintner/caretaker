import { Navigate } from 'react-router-dom'
import { Activity, LogIn } from 'lucide-react'
import { useUser } from '@/hooks/useUser'

export default function Login() {
  const { user, isLoading } = useUser()

  if (isLoading) {
    return (
      <div className="min-h-screen grid place-items-center text-sm text-[var(--color-muted-foreground)]">
        Loading…
      </div>
    )
  }

  if (user) return <Navigate to="/" replace />

  return (
    <div className="min-h-screen relative grid place-items-center p-6 overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(40rem 28rem at 30% 10%, var(--color-primary-soft), transparent 60%), radial-gradient(36rem 24rem at 80% 90%, var(--color-info-soft), transparent 60%)',
        }}
      />
      <div className="panel glass relative w-full max-w-sm p-8">
        <div
          aria-hidden
          className="h-10 w-10 rounded-[var(--radius-md)] grid place-items-center mb-4 text-[var(--color-primary-foreground)]"
          style={{
            background:
              'linear-gradient(135deg, var(--color-primary), var(--color-accent))',
            boxShadow: 'var(--shadow-glow)',
          }}
        >
          <Activity className="h-5 w-5" />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">Caretaker</h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          Admin dashboard. Sign in to continue.
        </p>
        <a
          href="/api/auth/login"
          className="mt-6 w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-[var(--radius-md)] bg-[var(--color-primary)] text-[var(--color-primary-foreground)] hover:opacity-90 text-sm font-medium transition-opacity"
        >
          <LogIn className="h-4 w-4" />
          Sign in with SSO
        </a>
      </div>
    </div>
  )
}
