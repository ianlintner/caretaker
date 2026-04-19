import { useState } from 'react'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import StatusBadge from '@/components/StatusBadge'
import Pagination from '@/components/Pagination'
import type { Paginated, TrackedPR } from '@/lib/types'

const COLUMNS: Column<TrackedPR>[] = [
  {
    key: 'number',
    header: '#',
    width: '80px',
    render: (r) => <span className="font-mono text-xs">#{r.number}</span>,
  },
  {
    key: 'state',
    header: 'State',
    render: (r) => <StatusBadge value={r.state} />,
  },
  {
    key: 'ownership',
    header: 'Ownership',
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.ownership_state}
      </span>
    ),
  },
  {
    key: 'readiness',
    header: 'Readiness',
    render: (r) => (
      <div className="flex items-center gap-2">
        <div className="w-20 h-1.5 bg-[var(--color-muted)] rounded-full overflow-hidden">
          <div
            className="h-full bg-[var(--color-primary)]"
            style={{ width: `${Math.round(r.readiness_score * 100)}%` }}
          />
        </div>
        <span className="text-xs tabular-nums">
          {(r.readiness_score * 100).toFixed(0)}%
        </span>
      </div>
    ),
  },
  {
    key: 'cycles',
    header: 'Cycles',
    render: (r) => <span className="text-xs">{r.fix_cycles}</span>,
  },
  {
    key: 'last_checked',
    header: 'Last checked',
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.last_checked ? new Date(r.last_checked).toLocaleString() : '—'}
      </span>
    ),
  },
]

const STATES = [
  '',
  'discovered',
  'ci_pending',
  'ci_passing',
  'ci_failing',
  'review_pending',
  'merge_ready',
  'merged',
  'escalated',
  'closed',
]

export default function PRs() {
  const [state, setState] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 50

  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (state) qs.set('state', state)
  const { data, isLoading } = useSWR<Paginated<TrackedPR>>(
    `/api/admin/prs?${qs}`,
  )

  return (
    <>
      <PageHeader
        title="Pull Requests"
        description="Pull requests tracked by the orchestrator."
        actions={
          <select
            value={state}
            onChange={(e) => {
              setState(e.target.value)
              setOffset(0)
            }}
            className="text-sm border border-[var(--color-border)] rounded-md px-3 py-1.5 bg-[var(--color-card)]"
          >
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s || 'All states'}
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
              empty="No PRs tracked."
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
