import { Navigate } from 'react-router-dom'
import { useUser } from '@/hooks/useUser'
import Layout from '@/components/Layout'

export default function RequireAuth() {
  const { user, isLoading, unauthenticated, error } = useUser()

  if (isLoading) {
    return (
      <div className="min-h-screen grid place-items-center text-sm text-[var(--color-muted-foreground)]">
        Loading…
      </div>
    )
  }

  if (unauthenticated || !user) {
    return <Navigate to="/login" replace />
  }

  if (error) {
    return (
      <div className="min-h-screen grid place-items-center p-6">
        <div className="max-w-md text-center">
          <h2 className="text-lg font-semibold">Unable to reach the server</h2>
          <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
            {error.message}
          </p>
        </div>
      </div>
    )
  }

  return <Layout />
}
