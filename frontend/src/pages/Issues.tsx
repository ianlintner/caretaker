import { useState } from 'react'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import StatusBadge from '@/components/StatusBadge'
import Pagination from '@/components/Pagination'
import type { Paginated, TrackedIssue } from '@/lib/types'

const COLUMNS: Column<TrackedIssue>[] = [
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
    key: 'classification',
    header: 'Classification',
    render: (r) => (
      <span className="text-xs">{r.classification || '—'}</span>
    ),
  },
  {
    key: 'assigned_pr',
    header: 'Assigned PR',
    render: (r) =>
      r.assigned_pr ? (
        <span className="font-mono text-xs">#{r.assigned_pr}</span>
      ) : (
        <span className="text-xs text-[var(--color-muted-foreground)]">—</span>
      ),
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
  'new',
  'triaged',
  'assigned',
  'in_progress',
  'pr_opened',
  'completed',
  'stale',
  'escalated',
  'closed',
]

export default function Issues() {
  const [state, setState] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 50

  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (state) qs.set('state', state)
  const { data, isLoading } = useSWR<Paginated<TrackedIssue>>(
    `/api/admin/issues?${qs}`,
  )

  return (
    <>
      <PageHeader
        title="Issues"
        description="Issues tracked by the orchestrator."
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
              empty="No issues tracked."
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
