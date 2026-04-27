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
import SearchInput from '@/components/SearchInput'
import { FilterBar, FilterSelect } from '@/components/FilterBar'
import type { Paginated, Skill } from '@/lib/types'

const CATEGORY_OPTIONS = [
  { value: 'ci', label: 'CI' },
  { value: 'issue', label: 'Issue' },
  { value: 'build', label: 'Build' },
  { value: 'security', label: 'Security' },
]

const COLUMNS: Column<Skill>[] = [
  {
    key: 'signature',
    header: 'Signature',
    sortable: true,
    sortValue: (r) => r.signature,
    render: (r) => <span className="font-mono text-xs">{r.signature}</span>,
  },
  {
    key: 'category',
    header: 'Category',
    sortable: true,
    sortValue: (r) => r.category,
    render: (r) => (
      <span className="inline-flex px-2 py-0.5 rounded-full bg-[var(--color-muted)] text-xs">
        {r.category}
      </span>
    ),
  },
  {
    key: 'success',
    header: 'Success / Fail',
    sortable: true,
    sortValue: (r) => {
      const total = r.success_count + r.fail_count
      return total === 0 ? -1 : r.success_count / total
    },
    render: (r) => (
      <span className="text-xs tabular-nums">
        <span className="text-emerald-600">{r.success_count}</span>
        <span className="text-[var(--color-muted-foreground)]"> / </span>
        <span className="text-rose-600">{r.fail_count}</span>
      </span>
    ),
  },
  {
    key: 'runs',
    header: 'Runs',
    sortable: true,
    sortValue: (r) => r.success_count + r.fail_count,
    render: (r) => (
      <span className="text-xs tabular-nums text-[var(--color-muted-foreground)]">
        {r.success_count + r.fail_count}
      </span>
    ),
  },
  {
    key: 'confidence',
    header: 'Confidence',
    sortable: true,
    sortValue: (r) => r.confidence,
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
    sortable: true,
    sortValue: (r) =>
      r.last_used_at ? new Date(r.last_used_at).getTime() : null,
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.last_used_at ? new Date(r.last_used_at).toLocaleString() : '—'}
      </span>
    ),
  },
]

export default function Skills() {
  const [category, setCategory] = useState<string | ''>('')
  const [search, setSearch] = useState('')
  const [minConfidence, setMinConfidence] = useState(0)
  const [offset, setOffset] = useState(0)
  const [sort, setSort] = useState<SortState>({
    key: 'confidence',
    dir: 'desc',
  })
  const limit = 50

  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  if (category) qs.set('category', category)
  const { data, isLoading } = useSWR<Paginated<Skill>>(
    `/api/admin/skills?${qs}`,
  )

  const visibleRows = useMemo(() => {
    const items = data?.items ?? []
    const q = search.trim().toLowerCase()
    let filtered = items
    if (q) {
      filtered = filtered.filter((r) =>
        r.signature.toLowerCase().includes(q),
      )
    }
    if (minConfidence > 0) {
      filtered = filtered.filter((r) => r.confidence * 100 >= minConfidence)
    }
    return sortRows(filtered, COLUMNS, sort)
  }, [data?.items, search, minConfidence, sort])

  return (
    <>
      <PageHeader
        title="Skills"
        description="Learned SOPs and their confidence scores."
      />
      <div className="p-6 space-y-4">
        <FilterBar>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search signature…"
            width="18rem"
          />
          <FilterSelect
            label="Category"
            value={category}
            options={CATEGORY_OPTIONS}
            onChange={(v) => {
              setCategory(v)
              setOffset(0)
            }}
            placeholder="All categories"
          />
          <label className="inline-flex items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
            <span>Min confidence</span>
            <input
              type="range"
              min={0}
              max={100}
              step={5}
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
              className="accent-[var(--color-primary)]"
            />
            <span className="tabular-nums w-10 text-right">
              {minConfidence}%
            </span>
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
                category || search || minConfidence > 0
                  ? 'No skills match these filters.'
                  : 'No skills recorded yet.'
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
