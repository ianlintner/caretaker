import { useState } from 'react'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import Pagination from '@/components/Pagination'
import type { Paginated, Skill } from '@/lib/types'

const COLUMNS: Column<Skill>[] = [
  {
    key: 'signature',
    header: 'Signature',
    render: (r) => <span className="font-mono text-xs">{r.signature}</span>,
  },
  {
    key: 'category',
    header: 'Category',
    render: (r) => (
      <span className="inline-flex px-2 py-0.5 rounded-full bg-[var(--color-muted)] text-xs">
        {r.category}
      </span>
    ),
  },
  {
    key: 'success',
    header: 'Success / Fail',
    render: (r) => (
      <span className="text-xs tabular-nums">
        <span className="text-emerald-600">{r.success_count}</span>
        <span className="text-[var(--color-muted-foreground)]"> / </span>
        <span className="text-rose-600">{r.fail_count}</span>
      </span>
    ),
  },
  {
    key: 'confidence',
    header: 'Confidence',
    render: (r) => (
      <div className="flex items-center gap-2">
        <div className="w-20 h-1.5 bg-[var(--color-muted)] rounded-full overflow-hidden">
          <div
            className="h-full bg-[var(--color-primary)]"
            style={{ width: `${Math.round(r.confidence * 100)}%` }}
          />
        </div>
        <span className="text-xs tabular-nums">
          {(r.confidence * 100).toFixed(0)}%
        </span>
      </div>
    ),
  },
  {
    key: 'last_used',
    header: 'Last used',
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.last_used_at ? new Date(r.last_used_at).toLocaleString() : '—'}
      </span>
    ),
  },
]

const CATEGORIES = ['', 'ci', 'issue', 'build', 'security']

export default function Skills() {
  const [category, setCategory] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 50

  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (category) qs.set('category', category)
  const { data, isLoading } = useSWR<Paginated<Skill>>(
    `/api/admin/skills?${qs}`,
  )

  return (
    <>
      <PageHeader
        title="Skills"
        description="Learned SOPs and their confidence scores."
        actions={
          <select
            value={category}
            onChange={(e) => {
              setCategory(e.target.value)
              setOffset(0)
            }}
            className="text-sm border border-[var(--color-border)] rounded-md px-3 py-1.5 bg-[var(--color-card)]"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c || 'All categories'}
              </option>
            ))}
          </select>
        }
      />
      <div className="p-8">
        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : (
          <>
            <DataTable
              columns={COLUMNS}
              rows={data?.items ?? []}
              empty="No skills recorded yet."
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
      </div>
    </>
  )
}
