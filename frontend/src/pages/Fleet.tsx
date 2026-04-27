import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import {
  DataTable,
  type Column,
  type SortState,
} from '@/components/DataTable'
import { sortRows } from '@/lib/tableSort'
import Pagination from '@/components/Pagination'
import StatusBadge from '@/components/StatusBadge'
import SearchInput from '@/components/SearchInput'
import {
  FilterBar,
  FilterSelect,
  FilterToggle,
} from '@/components/FilterBar'

type FleetClient = {
  repo: string
  caretaker_version: string
  last_seen: string
  first_seen: string
  last_mode: string
  enabled_agents: string[]
  last_goal_health: number | null
  last_error_count: number
  last_counters: Record<string, number>
  heartbeats_seen: number
}

type FleetList = {
  items: FleetClient[]
  total: number
  offset: number
  limit: number
}

type FleetSummary = {
  total_clients: number
  stale_clients: number
  stale_threshold_days: number
  version_distribution: Record<string, number>
}

const STALE_DAY_MS = 24 * 60 * 60 * 1000

function timeAgo(iso: string): string {
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return '—'
  const delta = Date.now() - t
  if (delta < 60_000) return 'just now'
  const m = Math.floor(delta / 60_000)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  return `${d}d ago`
}

const COLUMNS: Column<FleetClient>[] = [
  {
    key: 'repo',
    header: 'Repository',
    sortable: true,
    sortValue: (r) => r.repo,
    render: (r) => <span className="mono text-xs">{r.repo}</span>,
  },
  {
    key: 'version',
    header: 'Version',
    sortable: true,
    sortValue: (r) => r.caretaker_version,
    render: (r) => <span className="mono text-xs">{r.caretaker_version}</span>,
  },
  {
    key: 'mode',
    header: 'Last Mode',
    sortable: true,
    sortValue: (r) => r.last_mode,
    render: (r) => <StatusBadge value={r.last_mode} />,
  },
  {
    key: 'goal_health',
    header: 'Goal Health',
    sortable: true,
    sortValue: (r) => r.last_goal_health,
    render: (r) => (
      <span className="mono text-xs">
        {r.last_goal_health != null ? r.last_goal_health.toFixed(2) : '—'}
      </span>
    ),
  },
  {
    key: 'errors',
    header: 'Errors',
    sortable: true,
    sortValue: (r) => r.last_error_count,
    render: (r) => (
      <span
        className="mono text-xs"
        style={{
          color:
            r.last_error_count > 0 ? 'var(--color-destructive)' : undefined,
        }}
      >
        {r.last_error_count}
      </span>
    ),
  },
  {
    key: 'agents',
    header: 'Agents',
    sortable: true,
    sortValue: (r) => r.enabled_agents.length,
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.enabled_agents.length}
      </span>
    ),
  },
  {
    key: 'heartbeats',
    header: 'Heartbeats',
    sortable: true,
    sortValue: (r) => r.heartbeats_seen,
    render: (r) => <span className="mono text-xs">{r.heartbeats_seen}</span>,
  },
  {
    key: 'first_seen',
    header: 'First Seen',
    sortable: true,
    sortValue: (r) => new Date(r.first_seen).getTime() || null,
    render: (r) => (
      <span
        className="text-xs text-[var(--color-muted-foreground)]"
        title={r.first_seen}
      >
        {timeAgo(r.first_seen)}
      </span>
    ),
  },
  {
    key: 'last_seen',
    header: 'Last Seen',
    sortable: true,
    sortValue: (r) => new Date(r.last_seen).getTime() || null,
    render: (r) => (
      <span
        className="text-xs text-[var(--color-muted-foreground)]"
        title={r.last_seen}
      >
        {timeAgo(r.last_seen)}
      </span>
    ),
  },
]

export default function Fleet() {
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const [version, setVersion] = useState<string | ''>('')
  const [staleOnly, setStaleOnly] = useState(false)
  const [sort, setSort] = useState<SortState>({ key: 'last_seen', dir: 'desc' })
  const [now, setNow] = useState(() => Date.now())
  const limit = 50

  // Refresh "now" every 30s so stale-only filtering reflects elapsed time
  // without forcing impure Date.now() inside render.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(t)
  }, [])

  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  })
  if (version) qs.set('version', version)

  const { data: list, isLoading } = useSWR<FleetList>(
    `/api/admin/fleet?${qs}`,
  )
  const { data: summary } = useSWR<FleetSummary>('/api/admin/fleet/summary')

  const versions = summary?.version_distribution ?? {}
  const versionOptions = Object.entries(versions)
    .sort(([, a], [, b]) => b - a)
    .map(([v, n]) => ({ value: v, label: `${v} (${n})` }))
  const versionLabel = versionOptions
    .slice(0, 3)
    .map((o) => o.label)
    .join(' · ')

  const staleThresholdDays = summary?.stale_threshold_days ?? 7
  const staleThresholdMs = staleThresholdDays * STALE_DAY_MS

  const visibleRows = useMemo(() => {
    const items = list?.items ?? []
    const q = search.trim().toLowerCase()
    let filtered = items
    if (q) {
      filtered = filtered.filter((r) => r.repo.toLowerCase().includes(q))
    }
    if (staleOnly) {
      filtered = filtered.filter((r) => {
        const t = new Date(r.last_seen).getTime()
        if (!Number.isFinite(t)) return true
        return now - t >= staleThresholdMs
      })
    }
    return sortRows(filtered, COLUMNS, sort)
  }, [list?.items, search, staleOnly, staleThresholdMs, sort, now])

  return (
    <>
      <PageHeader
        title="Fleet"
        description="Consumer repositories that opted in to report heartbeats to this backend."
      />
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatPanel
            label="Registered repos"
            value={summary?.total_clients ?? '—'}
            hint="Reporting heartbeats to this backend"
            accentVar="--chart-1"
          />
          <StatPanel
            label="Stale"
            value={summary?.stale_clients ?? '—'}
            hint={`No heartbeat in ${staleThresholdDays} days`}
            accentVar="--chart-4"
          />
          <StatPanel
            label="Versions in fleet"
            value={Object.keys(versions).length || '—'}
            hint={versionLabel || 'No heartbeats yet'}
            accentVar="--chart-2"
          />
          <StatPanel
            label="Opt-in status"
            value={
              summary?.total_clients != null
                ? summary.total_clients > 0
                  ? 'Active'
                  : 'Waiting'
                : '—'
            }
            hint="Heartbeat endpoint: /api/fleet/heartbeat"
            accentVar="--chart-3"
          />
        </div>

        <FilterBar>
          <SearchInput
            value={search}
            onChange={(v) => {
              setSearch(v)
              setOffset(0)
            }}
            placeholder="Search repository…"
          />
          <FilterSelect
            label="Version"
            value={version}
            options={versionOptions}
            onChange={(v) => {
              setVersion(v)
              setOffset(0)
            }}
            placeholder="All versions"
          />
          <FilterToggle
            active={staleOnly}
            onChange={(v) => {
              setStaleOnly(v)
              setOffset(0)
            }}
          >
            Stale only ({staleThresholdDays}d+)
          </FilterToggle>
          <span className="text-xs text-[var(--color-muted-foreground)] ml-auto">
            {visibleRows.length} of {list?.total ?? 0}
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
              onRowClick={(r) => {
                const [owner, repo] = r.repo.split('/')
                if (owner && repo) {
                  navigate(`/fleet/${owner}/${repo}`)
                }
              }}
              empty={
                search || staleOnly || version
                  ? 'No repositories match these filters.'
                  : "No consumer repositories have registered yet. Set caretaker's fleet_registry.enabled = true and point fleet_registry.endpoint at this backend to opt in."
              }
            />
            {list && list.total > limit && (
              <Pagination
                offset={list.offset}
                limit={list.limit}
                total={list.total}
                onChange={setOffset}
              />
            )}
          </>
        )}
      </div>
    </>
  )
}
