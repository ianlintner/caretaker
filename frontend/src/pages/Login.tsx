import { Navigate } from 'react-router-dom'
import { LogIn } from 'lucide-react'
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
    <div className="min-h-screen grid place-items-center p-6">
      <div className="w-full max-w-sm border border-[var(--color-border)] rounded-xl p-8 bg-[var(--color-card)]">
        <h1 className="text-2xl font-semibold tracking-tight">Caretaker</h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          Admin dashboard. Sign in to continue.
        </p>
        <a
          href="/api/auth/login"
          className="mt-6 w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-md bg-[var(--color-primary)] text-[var(--color-primary-foreground)] hover:opacity-90 text-sm font-medium"
        >
          <LogIn className="h-4 w-4" />
          Sign in with SSO
        </a>
      </div>
    </div>
  )
}
