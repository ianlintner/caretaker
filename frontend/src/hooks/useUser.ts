import useSWR from 'swr'
import { fetcher, ApiError } from '@/lib/api'
import type { UserInfo } from '@/lib/types'

export function useUser() {
  const { data, error, isLoading, mutate } = useSWR<UserInfo>(
    '/api/auth/me',
    fetcher,
    { shouldRetryOnError: false },
  )
  const unauthenticated = error instanceof ApiError && error.status === 401
  return {
    user: data,
    isLoading,
    unauthenticated,
    error: error && !unauthenticated ? error : null,
    refresh: mutate,
  }
}
