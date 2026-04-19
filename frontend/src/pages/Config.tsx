import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'

export default function Config() {
  const { data, isLoading } = useSWR<Record<string, unknown>>('/api/admin/config')

  return (
    <>
      <PageHeader
        title="Configuration"
        description="Active configuration (secrets redacted)."
      />
      <div className="p-8">
        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : (
          <pre className="border border-[var(--color-border)] rounded-lg p-4 bg-[var(--color-card)] text-xs font-mono overflow-auto max-h-[calc(100vh-14rem)]">
            {JSON.stringify(data, null, 2)}
          </pre>
        )}
      </div>
    </>
  )
}
