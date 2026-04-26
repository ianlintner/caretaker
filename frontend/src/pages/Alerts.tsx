import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import {
  DataTable,
  type Column,
  type SortState,
} from '@/components/DataTable'
import { sortRows } from '@/lib/tableSort'
import StatusBadge from '@/components/StatusBadge'
import SearchInput from '@/components/SearchInput'
import {
  FilterBar,
  FilterSelect,
  FilterToggle,
} from '@/components/FilterBar'
import type {
  FleetAlert,
  FleetAlertKind,
  FleetAlertList,
  FleetAlertSeverity,
} from '@/lib/types'

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

const KIND_LABELS: Record<FleetAlertKind, string> = {
  goal_health_regression: 'Goal health regression',
  error_spike: 'Error spike',
  ghosted: 'Ghosted (no heartbeats)',
  scope_gap: 'Scope gap',
}

const SEVERITY_RANK: Record<FleetAlertSeverity, number> = {
  critical: 0,
  warning: 1,
}

const KIND_OPTIONS: { value: FleetAlertKind; label: string }[] = [
  { value: 'goal_health_regression', label: KIND_LABELS.goal_health_regression },
  { value: 'error_spike', label: KIND_LABELS.error_spike },
  { value: 'ghosted', label: KIND_LABELS.ghosted },
  { value: 'scope_gap', label: KIND_LABELS.scope_gap },
]

const SEVERITY_OPTIONS: { value: FleetAlertSeverity; label: string }[] = [
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
]

export default function Alerts() {
  const [openOnly, setOpenOnly] = useState(true)
  const [search, setSearch] = useState('')
  const [severity, setSeverity] = useState<FleetAlertSeverity | ''>('')
  const [kind, setKind] = useState<FleetAlertKind | ''>('')
  const [sort, setSort] = useState<SortState>({
    key: 'opened_at',
    dir: 'desc',
  })

  const url = `/api/admin/fleet/alerts${openOnly ? '?open=true' : ''}`
  const { data, isLoading, error } = useSWR<FleetAlertList>(url, {
    refreshInterval: 60_000,
  })

  const alerts = useMemo(() => data?.items ?? [], [data?.items])
  const stats = useMemo(() => {
    const open = alerts.filter((a) => a.resolved_at === null)
    const critical = open.filter((a) => a.severity === 'critical').length
    const warning = open.length - critical
    const repos = new Set(open.map((a) => a.repo)).size
    return { open: open.length, critical, warning, repos }
  }, [alerts])

  const columns: Column<FleetAlert>[] = useMemo(
    () => [
      {
        key: 'severity',
        header: 'Severity',
        sortable: true,
        sortValue: (a) => SEVERITY_RANK[a.severity] ?? 99,
        render: (a) => <StatusBadge value={a.severity} />,
      },
      {
        key: 'repo',
        header: 'Repository',
        sortable: true,
        sortValue: (a) => a.repo,
        render: (a) => {
          const [owner, repo] = a.repo.split('/')
          if (!owner || !repo) {
            return <span className="mono text-xs">{a.repo}</span>
          }
          return (
            <Link
              to={`/fleet/${owner}/${repo}`}
              className="mono text-xs underline hover:opacity-80"
            >
              {a.repo}
            </Link>
          )
        },
      },
      {
        key: 'kind',
        header: 'Kind',
        sortable: true,
        sortValue: (a) => KIND_LABELS[a.kind] ?? a.kind,
        render: (a) => (
          <span className="text-xs">{KIND_LABELS[a.kind] ?? a.kind}</span>
        ),
      },
      {
        key: 'summary',
        header: 'Summary',
        render: (a) => <span className="text-xs">{a.summary}</span>,
      },
      {
        key: 'opened_at',
        header: 'Opened',
        sortable: true,
        sortValue: (a) => new Date(a.opened_at).getTime() || null,
        render: (a) => (
          <span
            className="text-xs text-[var(--color-muted-foreground)]"
            title={a.opened_at}
          >
            {timeAgo(a.opened_at)}
          </span>
        ),
      },
      {
        key: 'resolved_at',
        header: 'Status',
        sortable: true,
        sortValue: (a) => (a.resolved_at === null ? 0 : 1),
        render: (a) =>
          a.resolved_at === null ? (
            <span
              className="text-xs"
              style={{ color: 'var(--color-warning)' }}
            >
              open
            </span>
          ) : (
            <span
              className="text-xs text-[var(--color-muted-foreground)]"
              title={a.resolved_at}
            >
              resolved {timeAgo(a.resolved_at)}
            </span>
          ),
      },
    ],
    [],
  )

  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    let filtered = alerts
    if (q) {
      filtered = filtered.filter(
        (a) =>
          a.repo.toLowerCase().includes(q) ||
          a.summary.toLowerCase().includes(q),
      )
    }
    if (severity) {
      filtered = filtered.filter((a) => a.severity === severity)
    }
    if (kind) {
      filtered = filtered.filter((a) => a.kind === kind)
    }
    return sortRows(filtered, columns, sort)
  }, [alerts, search, severity, kind, columns, sort])

  return (
    <>
      <PageHeader
        title="Fleet Alerts"
        description="Aggregated alerts across all reporting repositories. Evaluated when this page loads or the AlertsBanner polls (every 60s)."
      />
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatPanel
            label="Open alerts"
            value={stats.open}
            hint="Currently unresolved"
            accentVar="--chart-1"
          />
          <StatPanel
            label="Critical"
            value={stats.critical}
            hint="Highest severity"
            accentVar="--chart-4"
          />
          <StatPanel
            label="Warning"
            value={stats.warning}
            hint="Lower severity"
            accentVar="--chart-2"
          />
          <StatPanel
            label="Affected repos"
            value={stats.repos}
            hint="Distinct repositories"
            accentVar="--chart-3"
          />
        </div>

        <FilterBar>
          <FilterToggle active={openOnly} onChange={setOpenOnly}>
            {openOnly ? 'Open only' : 'All (incl. resolved)'}
          </FilterToggle>
          <SearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search repo or summary…"
          />
          <FilterSelect
            label="Severity"
            value={severity}
            options={SEVERITY_OPTIONS}
            onChange={setSeverity}
            placeholder="All severities"
          />
          <FilterSelect
            label="Kind"
            value={kind}
            options={KIND_OPTIONS}
            onChange={setKind}
            placeholder="All kinds"
          />
          <span className="text-xs text-[var(--color-muted-foreground)] ml-auto">
            {visibleRows.length} of {alerts.length}
          </span>
        </FilterBar>

        {error ? (
          <p className="text-sm" style={{ color: 'var(--color-destructive)' }}>
            Failed to load alerts.
          </p>
        ) : isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">
            Loading…
          </p>
        ) : (
          <DataTable
            columns={columns}
            rows={visibleRows}
            sort={sort}
            onSortChange={setSort}
            empty={
              search || severity || kind
                ? 'No alerts match these filters.'
                : openOnly
                  ? 'No open alerts. The fleet looks healthy.'
                  : 'No alerts have been evaluated yet.'
            }
          />
        )}
      </div>
    </>
  )
}
