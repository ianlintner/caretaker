import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import { DataTable, type Column } from '@/components/DataTable'
import StatusBadge from '@/components/StatusBadge'
import type { FleetAlert, FleetAlertList } from '@/lib/types'

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

const KIND_LABELS: Record<FleetAlert['kind'], string> = {
  goal_health_regression: 'Goal health regression',
  error_spike: 'Error spike',
  ghosted: 'Ghosted (no heartbeats)',
  scope_gap: 'Scope gap',
}

export default function Alerts() {
  const [filterOpen, setFilterOpen] = useState(true)
  const url = `/api/admin/fleet/alerts${filterOpen ? '?open=true' : ''}`
  const { data, isLoading, error } = useSWR<FleetAlertList>(url, {
    refreshInterval: 60_000,
  })

  const alerts = data?.items ?? []
  const stats = useMemo(() => {
    const open = alerts.filter((a) => a.resolved_at === null)
    const critical = open.filter((a) => a.severity === 'critical').length
    const warning = open.length - critical
    const repos = new Set(open.map((a) => a.repo)).size
    return { open: open.length, critical, warning, repos }
  }, [alerts])

  const columns: Column<FleetAlert>[] = [
    {
      key: 'severity',
      header: 'Severity',
      render: (a) => <StatusBadge value={a.severity} />,
    },
    {
      key: 'repo',
      header: 'Repository',
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
  ]

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

        <div className="flex items-center gap-3 text-xs">
          <button
            type="button"
            onClick={() => setFilterOpen(true)}
            className="px-2 py-1 rounded border"
            style={{
              borderColor: filterOpen
                ? 'var(--color-primary)'
                : 'var(--color-border)',
              background: filterOpen
                ? 'var(--color-primary-soft)'
                : 'transparent',
            }}
          >
            Open only
          </button>
          <button
            type="button"
            onClick={() => setFilterOpen(false)}
            className="px-2 py-1 rounded border"
            style={{
              borderColor: !filterOpen
                ? 'var(--color-primary)'
                : 'var(--color-border)',
              background: !filterOpen
                ? 'var(--color-primary-soft)'
                : 'transparent',
            }}
          >
            All (incl. resolved)
          </button>
        </div>

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
            rows={alerts}
            empty={
              filterOpen
                ? 'No open alerts. The fleet looks healthy.'
                : 'No alerts have been evaluated yet.'
            }
          />
        )}
      </div>
    </>
  )
}
