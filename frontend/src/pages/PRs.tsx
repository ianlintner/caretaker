import { useMemo, useState } from 'react'
import useSWR from 'swr'
import { CheckCircle2 } from 'lucide-react'
import PageHeader from '@/components/PageHeader'
import {
  DataTable,
  type Column,
  type SortState,
} from '@/components/DataTable'
import { sortRows } from '@/lib/tableSort'
import StatusBadge from '@/components/StatusBadge'
import Pagination from '@/components/Pagination'
import SearchInput from '@/components/SearchInput'
import { FilterBar, FilterSelect } from '@/components/FilterBar'
import type { Paginated, TrackedPR } from '@/lib/types'

const STATE_OPTIONS = [
  'discovered',
  'ci_pending',
  'ci_passing',
  'ci_failing',
  'review_pending',
  'review_approved',
  'review_changes_requested',
  'fix_requested',
  'fix_in_progress',
  'merge_ready',
  'merged',
  'escalated',
  'closed',
].map((v) => ({ value: v, label: v.replace(/_/g, ' ') }))

const OWNERSHIP_OPTIONS = [
  { value: 'unowned', label: 'Unowned' },
  { value: 'owned', label: 'Owned' },
  { value: 'released', label: 'Released' },
  { value: 'escalated', label: 'Escalated' },
]

const COLUMNS: Column<TrackedPR>[] = [
  {
    key: 'number',
    header: '#',
    width: '80px',
    sortable: true,
    sortValue: (r) => r.number,
    render: (r) => <span className="font-mono text-xs">#{r.number}</span>,
  },
  {
    key: 'state',
    header: 'State',
    sortable: true,
    sortValue: (r) => r.state,
    render: (r) => <StatusBadge value={r.state} />,
  },
  {
    key: 'ownership',
    header: 'Ownership',
    sortable: true,
    sortValue: (r) => r.ownership_state,
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.ownership_state}
      </span>
    ),
  },
  {
    key: 'readiness',
    header: 'Readiness',
    sortable: true,
    sortValue: (r) => r.readiness_score,
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
    sortable: true,
    sortValue: (r) => r.fix_cycles,
    render: (r) => <span className="text-xs">{r.fix_cycles}</span>,
  },
  {
    key: 'caretaker_touched',
    header: 'Caretaker',
    width: '90px',
    sortable: true,
    sortValue: (r) =>
      r.caretaker_merged ? 2 : r.caretaker_touched ? 1 : 0,
    render: (r) =>
      r.caretaker_merged ? (
        <span
          className="inline-flex items-center gap-1 text-xs"
          style={{ color: 'var(--color-success, #10b981)' }}
          title="Caretaker merged"
        >
          <CheckCircle2 className="h-3.5 w-3.5" /> merged
        </span>
      ) : r.caretaker_touched ? (
        <span
          className="text-xs text-[var(--color-muted-foreground)]"
          title="Caretaker touched this PR"
        >
          touched
        </span>
      ) : (
        <span className="text-xs text-[var(--color-muted-foreground)]">—</span>
      ),
  },
  {
    key: 'merged_at',
    header: 'Merged',
    sortable: true,
    sortValue: (r) => (r.merged_at ? new Date(r.merged_at).getTime() : null),
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.merged_at ? new Date(r.merged_at).toLocaleDateString() : '—'}
      </span>
    ),
  },
  {
    key: 'last_checked',
    header: 'Last checked',
    sortable: true,
    sortValue: (r) =>
      r.last_checked ? new Date(r.last_checked).getTime() : null,
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.last_checked ? new Date(r.last_checked).toLocaleString() : '—'}
      </span>
    ),
  },
]

export default function PRs() {
  const [state, setState] = useState<string | ''>('')
  const [ownership, setOwnership] = useState<string | ''>('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<SortState>(null)
  const limit = 50

  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  if (state) qs.set('state', state)
  if (ownership) qs.set('ownership', ownership)
  const { data, isLoading } = useSWR<Paginated<TrackedPR>>(
    `/api/admin/prs?${qs}`,
  )

  const visibleRows = useMemo(() => {
    const items = data?.items ?? []
    const q = search.trim().toLowerCase()
    let filtered = items
    if (q) {
      filtered = filtered.filter((r) => {
        if (String(r.number).includes(q)) return true
        if (r.notes?.toLowerCase().includes(q)) return true
        if (r.readiness_summary?.toLowerCase().includes(q)) return true
        return false
      })
    }
    return sortRows(filtered, COLUMNS, sort)
  }, [data?.items, search, sort])

  return (
    <>
      <PageHeader
        title="Pull Requests"
        description="Pull requests tracked by the orchestrator."
      />
      <div className="p-6 space-y-4">
        <FilterBar>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search by # or notes…"
          />
          <FilterSelect
            label="State"
            value={state}
            options={STATE_OPTIONS}
            onChange={(v) => {
              setState(v)
              setOffset(0)
            }}
            placeholder="All states"
          />
          <FilterSelect
            label="Ownership"
            value={ownership}
            options={OWNERSHIP_OPTIONS}
            onChange={(v) => {
              setOwnership(v)
              setOffset(0)
            }}
            placeholder="All ownership"
          />
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
                state || ownership || search
                  ? 'No PRs match these filters.'
                  : 'No PRs tracked.'
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
