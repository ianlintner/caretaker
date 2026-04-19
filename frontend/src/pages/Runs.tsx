import { useState } from 'react'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import Pagination from '@/components/Pagination'
import type { Paginated, RunSummary } from '@/lib/types'

const COLUMNS: Column<RunSummary>[] = [
  {
    key: 'run_at',
    header: 'Time',
    render: (r) => (
      <span className="text-xs tabular-nums">
        {new Date(r.run_at).toLocaleString()}
      </span>
    ),
  },
  {
    key: 'mode',
    header: 'Mode',
    render: (r) => <span className="text-xs">{r.mode}</span>,
  },
  {
    key: 'prs_monitored',
    header: 'PRs monitored',
    render: (r) => <span className="tabular-nums">{r.prs_monitored}</span>,
  },
  {
    key: 'prs_merged',
    header: 'Merged',
    render: (r) => <span className="tabular-nums">{r.prs_merged}</span>,
  },
  {
    key: 'issues_triaged',
    header: 'Issues triaged',
    render: (r) => <span className="tabular-nums">{r.issues_triaged}</span>,
  },
  {
    key: 'escalations',
    header: 'Escalations',
    render: (r) => (
      <span className="tabular-nums">
        {r.prs_escalated + r.issues_escalated}
      </span>
    ),
  },
  {
    key: 'errors',
    header: 'Errors',
    render: (r) => {
      const n = r.errors?.length ?? 0
      return (
        <span
          className={
            n > 0 ? 'text-rose-600 font-medium tabular-nums' : 'tabular-nums'
          }
        >
          {n}
        </span>
      )
    },
  },
]

export default function Runs() {
  const [offset, setOffset] = useState(0)
  const limit = 20

  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  const { data, isLoading } = useSWR<Paginated<RunSummary>>(
    `/api/admin/runs?${qs}`,
  )

  return (
    <>
      <PageHeader
        title="Run history"
        description="Per-run summary of orchestrator activity."
      />
      <div className="p-8">
        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : (
          <>
            <DataTable
              columns={COLUMNS}
              rows={data?.items ?? []}
              empty="No runs recorded yet."
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
