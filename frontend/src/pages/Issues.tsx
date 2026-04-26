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
import type { Paginated, TrackedIssue } from '@/lib/types'

const STATE_OPTIONS = [
  'new',
  'triaged',
  'assigned',
  'in_progress',
  'pr_opened',
  'completed',
  'stale',
  'escalated',
  'closed',
].map((v) => ({ value: v, label: v.replace(/_/g, ' ') }))

const COLUMNS: Column<TrackedIssue>[] = [
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
    key: 'classification',
    header: 'Classification',
    sortable: true,
    sortValue: (r) => r.classification ?? '',
    render: (r) => <span className="text-xs">{r.classification || '—'}</span>,
  },
  {
    key: 'assigned_pr',
    header: 'Assigned PR',
    sortable: true,
    sortValue: (r) => r.assigned_pr ?? null,
    render: (r) =>
      r.assigned_pr ? (
        <span className="font-mono text-xs">#{r.assigned_pr}</span>
      ) : (
        <span className="text-xs text-[var(--color-muted-foreground)]">—</span>
      ),
  },
  {
    key: 'caretaker_touched',
    header: 'Caretaker',
    width: '90px',
    sortable: true,
    sortValue: (r) =>
      r.caretaker_closed ? 2 : r.caretaker_touched ? 1 : 0,
    render: (r) =>
      r.caretaker_closed ? (
        <span
          className="inline-flex items-center gap-1 text-xs"
          style={{ color: 'var(--color-success, #10b981)' }}
          title="Caretaker closed"
        >
          <CheckCircle2 className="h-3.5 w-3.5" /> closed
        </span>
      ) : r.caretaker_touched ? (
        <span
          className="text-xs text-[var(--color-muted-foreground)]"
          title="Caretaker touched this issue"
        >
          touched
        </span>
      ) : (
        <span className="text-xs text-[var(--color-muted-foreground)]">—</span>
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

export default function Issues() {
  const [state, setState] = useState<string | ''>('')
  const [classification, setClassification] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<SortState>(null)
  const limit = 50

  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  if (state) qs.set('state', state)
  if (classification.trim()) qs.set('classification', classification.trim())
  const { data, isLoading } = useSWR<Paginated<TrackedIssue>>(
    `/api/admin/issues?${qs}`,
  )

  const visibleRows = useMemo(() => {
    const items = data?.items ?? []
    const q = search.trim().toLowerCase()
    if (!q) return sortRows(items, COLUMNS, sort)
    const filtered = items.filter((r) => {
      if (String(r.number).includes(q)) return true
      if (r.classification?.toLowerCase().includes(q)) return true
      return false
    })
    return sortRows(filtered, COLUMNS, sort)
  }, [data?.items, search, sort])

  return (
    <>
      <PageHeader
        title="Issues"
        description="Issues tracked by the orchestrator."
      />
      <div className="p-6 space-y-4">
        <FilterBar>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search by # or class…"
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
          <label className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
            <span>Classification</span>
            <input
              type="text"
              value={classification}
              onChange={(e) => {
                setClassification(e.target.value)
                setOffset(0)
              }}
              placeholder="exact match…"
              className="text-sm border border-[var(--color-border)] rounded-md px-2.5 py-1.5 bg-[var(--color-card)] text-[var(--color-foreground)] w-40"
            />
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
                state || classification || search
                  ? 'No issues match these filters.'
                  : 'No issues tracked.'
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
