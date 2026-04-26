import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import { DataTable, type Column } from '@/components/DataTable'
import Pagination from '@/components/Pagination'
import StatusBadge from '@/components/StatusBadge'

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
    render: (r) => <span className="mono text-xs">{r.repo}</span>,
  },
  {
    key: 'version',
    header: 'Version',
    render: (r) => <span className="mono text-xs">{r.caretaker_version}</span>,
  },
  {
    key: 'mode',
    header: 'Last Mode',
    render: (r) => <StatusBadge value={r.last_mode} />,
  },
  {
    key: 'goal_health',
    header: 'Goal Health',
    render: (r) => (
      <span className="mono text-xs">
        {r.last_goal_health != null ? r.last_goal_health.toFixed(2) : '—'}
      </span>
    ),
  },
  {
    key: 'errors',
    header: 'Errors',
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
    render: (r) => (
      <span className="text-xs text-[var(--color-muted-foreground)]">
        {r.enabled_agents.length}
      </span>
    ),
  },
  {
    key: 'heartbeats',
    header: 'Heartbeats',
    render: (r) => <span className="mono text-xs">{r.heartbeats_seen}</span>,
  },
  {
    key: 'last_seen',
    header: 'Last Seen',
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
  const limit = 50

  const { data: list, isLoading } = useSWR<FleetList>(
    `/api/admin/fleet?offset=${offset}&limit=${limit}`,
  )
  const { data: summary } = useSWR<FleetSummary>('/api/admin/fleet/summary')

  const versions = summary?.version_distribution ?? {}
  const versionLabel = Object.entries(versions)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 3)
    .map(([v, n]) => `${v} (${n})`)
    .join(' · ')

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
            hint={`No heartbeat in ${summary?.stale_threshold_days ?? 7} days`}
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

        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">
            Loading…
          </p>
        ) : (
          <>
            <DataTable
              columns={COLUMNS}
              rows={list?.items ?? []}
              onRowClick={(r) => {
                const [owner, repo] = r.repo.split('/')
                if (owner && repo) {
                  navigate(`/fleet/${owner}/${repo}`)
                }
              }}
              empty="No consumer repositories have registered yet. Set caretaker's fleet_registry.enabled = true and point fleet_registry.endpoint at this backend to opt in."
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
