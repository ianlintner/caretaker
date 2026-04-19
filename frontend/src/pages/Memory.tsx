import { useState } from 'react'
import useSWR from 'swr'
import { useNavigate, useParams } from 'react-router-dom'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import Pagination from '@/components/Pagination'
import type { MemoryEntry, MemoryNamespace, Paginated } from '@/lib/types'

function NamespaceList() {
  const navigate = useNavigate()
  const { data, isLoading } = useSWR<MemoryNamespace[]>('/api/admin/memory')

  const columns: Column<MemoryNamespace>[] = [
    {
      key: 'namespace',
      header: 'Namespace',
      render: (r) => <span className="font-mono text-xs">{r.namespace}</span>,
    },
    {
      key: 'count',
      header: 'Keys',
      render: (r) => <span className="tabular-nums">{r.key_count}</span>,
    },
  ]

  if (isLoading) {
    return <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
  }

  return (
    <DataTable
      columns={columns}
      rows={data ?? []}
      empty="No memory namespaces."
      onRowClick={(r) => navigate(`/memory/${encodeURIComponent(r.namespace)}`)}
    />
  )
}

function EntryList({ namespace }: { namespace: string }) {
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const limit = 50
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  const { data, isLoading } = useSWR<Paginated<MemoryEntry>>(
    `/api/admin/memory/${encodeURIComponent(namespace)}?${qs}`,
  )

  const columns: Column<MemoryEntry>[] = [
    {
      key: 'key',
      header: 'Key',
      render: (r) => (
        <span className="font-mono text-xs break-all">{r.key}</span>
      ),
    },
    {
      key: 'value',
      header: 'Value',
      render: (r) => (
        <span className="text-xs text-[var(--color-muted-foreground)] line-clamp-2 block">
          {r.value}
        </span>
      ),
    },
    {
      key: 'updated',
      header: 'Updated',
      width: '180px',
      render: (r) => (
        <span className="text-xs text-[var(--color-muted-foreground)]">
          {r.updated_at ? new Date(r.updated_at).toLocaleString() : '—'}
        </span>
      ),
    },
  ]

  return (
    <>
      <button
        onClick={() => navigate('/memory')}
        className="mb-4 text-xs text-[var(--color-primary)] hover:underline"
      >
        ← All namespaces
      </button>
      {isLoading ? (
        <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
      ) : (
        <>
          <DataTable
            columns={columns}
            rows={data?.items ?? []}
            empty="No entries."
          />
          {data && (
            <Pagination
              offset={data.offset}
              limit={data.limit}
              total={data.total}
              onChange={setOffset}
            />
          )}
        </>
      )}
    </>
  )
}

export default function Memory() {
  const { namespace } = useParams<{ namespace?: string }>()
  return (
    <>
      <PageHeader
        title={namespace ? `Memory: ${namespace}` : 'Memory'}
        description={
          namespace
            ? 'Key/value entries in this namespace.'
            : 'Namespaces in the memory store.'
        }
      />
      <div className="p-8">
        {namespace ? <EntryList namespace={namespace} /> : <NamespaceList />}
      </div>
    </>
  )
}
