import { useMemo, useState } from 'react'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import {
  DataTable,
  type Column,
  type SortState,
} from '@/components/DataTable'
import { sortRows } from '@/lib/tableSort'
import Pagination from '@/components/Pagination'
import { FilterBar, FilterSelect } from '@/components/FilterBar'
import type { Paginated, RunSummary } from '@/lib/types'

const COLUMNS: Column<RunSummary>[] = [
  {
    key: 'run_at',
    header: 'Time',
    sortable: true,
    sortValue: (r) => new Date(r.run_at).getTime() || null,
    render: (r) => (
      <span className="text-xs tabular-nums">
        {new Date(r.run_at).toLocaleString()}
      </span>
    ),
  },
  {
    key: 'mode',
    header: 'Mode',
    sortable: true,
    sortValue: (r) => r.mode,
    render: (r) => <span className="text-xs">{r.mode}</span>,
  },
  {
    key: 'prs_monitored',
    header: 'PRs monitored',
    sortable: true,
    sortValue: (r) => r.prs_monitored,
    render: (r) => <span className="tabular-nums">{r.prs_monitored}</span>,
  },
  {
    key: 'prs_merged',
    header: 'Merged',
    sortable: true,
    sortValue: (r) => r.prs_merged,
    render: (r) => <span className="tabular-nums">{r.prs_merged}</span>,
  },
  {
    key: 'issues_triaged',
    header: 'Issues triaged',
    sortable: true,
    sortValue: (r) => r.issues_triaged,
    render: (r) => <span className="tabular-nums">{r.issues_triaged}</span>,
  },
  {
    key: 'escalations',
    header: 'Escalations',
    sortable: true,
    sortValue: (r) => r.prs_escalated + r.issues_escalated,
    render: (r) => (
      <span className="tabular-nums">
        {r.prs_escalated + r.issues_escalated}
      </span>
    ),
  },
  {
    key: 'goal_health',
    header: 'Goal Health',
    sortable: true,
    sortValue: (r) => r.goal_health,
    render: (r) => (
      <span className="tabular-nums text-xs">
        {r.goal_health != null ? r.goal_health.toFixed(2) : '—'}
      </span>
    ),
  },
  {
    key: 'errors',
    header: 'Errors',
    sortable: true,
    sortValue: (r) => r.errors?.length ?? 0,
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
  const [mode, setMode] = useState<string | ''>('')
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [sort, setSort] = useState<SortState>(null)
  const limit = 20

  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  const { data, isLoading } = useSWR<Paginated<RunSummary>>(
    `/api/admin/runs?${qs}`,
  )

  const items = useMemo(() => data?.items ?? [], [data?.items])
  // Build mode dropdown options dynamically from current page so we don't
  // hardcode a list that drifts from server-side reality.
  const modeOptions = useMemo(() => {
    const set = new Set<string>()
    for (const r of items) if (r.mode) set.add(r.mode)
    return Array.from(set)
      .sort()
      .map((v) => ({ value: v, label: v }))
  }, [items])

  const visibleRows = useMemo(() => {
    let filtered = items
    if (mode) filtered = filtered.filter((r) => r.mode === mode)
    if (errorsOnly) filtered = filtered.filter((r) => (r.errors?.length ?? 0) > 0)
    return sortRows(filtered, COLUMNS, sort)
  }, [items, mode, errorsOnly, sort])

  return (
    <>
      <PageHeader
        title="Run history"
        description="Per-run summary of orchestrator activity."
      />
      <div className="p-6 space-y-4">
        <FilterBar>
          <FilterSelect
            label="Mode"
            value={mode}
            options={modeOptions}
            onChange={setMode}
            placeholder="All modes"
          />
          <label className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
            <input
              type="checkbox"
              checked={errorsOnly}
              onChange={(e) => setErrorsOnly(e.target.checked)}
              className="accent-[var(--color-primary)]"
            />
            <span>Errors only</span>
          </label>
          <span className="text-xs text-[var(--color-muted-foreground)] ml-auto">
            {visibleRows.length} of {data?.total ?? 0}
          </span>
        </FilterBar>

        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">
            Loading…
          </p>
        ) : (
          <>
            <DataTable
              columns={COLUMNS}
              rows={visibleRows}
              sort={sort}
              onSortChange={setSort}
              empty={
                mode || errorsOnly
                  ? 'No runs match these filters.'
                  : 'No runs recorded yet.'
              }
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
