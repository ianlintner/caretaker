import { Link, useParams } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import StatusBadge from '@/components/StatusBadge'
import { DataTable, type Column } from '@/components/DataTable'
import JsonViewer from '@/components/JsonViewer'
import type { FleetClientDetail } from '@/lib/types'

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
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

type HeartbeatRow = {
  run_at: string
  mode: string
  goal_health: number | null
  error_count: number
  enabled_agents: string[]
  counters: Record<string, number>
  raw: Record<string, unknown>
}

function toHeartbeatRow(item: Record<string, unknown>): HeartbeatRow {
  const run_at =
    typeof item.run_at === 'string' ? item.run_at : String(item.run_at ?? '')
  const mode = typeof item.mode === 'string' ? item.mode : '—'
  const goal_health =
    typeof item.last_goal_health === 'number'
      ? item.last_goal_health
      : typeof item.goal_health === 'number'
        ? item.goal_health
        : null
  const error_count =
    typeof item.last_error_count === 'number'
      ? item.last_error_count
      : typeof item.error_count === 'number'
        ? item.error_count
        : 0
  const enabled_agents = Array.isArray(item.enabled_agents)
    ? (item.enabled_agents as string[])
    : []
  const counters = (item.last_counters ??
    item.counters ??
    {}) as Record<string, number>
  return { run_at, mode, goal_health, error_count, enabled_agents, counters, raw: item }
}

export default function FleetDetail() {
  const { owner, repo } = useParams<{ owner: string; repo: string }>()
  const slug = `${owner}/${repo}`
  const url = `/api/admin/fleet/${owner}/${repo}?include_history=true`
  const { data, error, isLoading } = useSWR<FleetClientDetail>(url, {
    refreshInterval: 60_000,
  })

  const history = (data?.history ?? []).map(toHeartbeatRow)
  // Most-recent first for the table.
  const orderedHistory = [...history].reverse()

  const columns: Column<HeartbeatRow>[] = [
    {
      key: 'run_at',
      header: 'Run',
      render: (h) => (
        <span className="text-xs text-[var(--color-muted-foreground)]" title={h.run_at}>
          {timeAgo(h.run_at)}
        </span>
      ),
    },
    {
      key: 'mode',
      header: 'Mode',
      render: (h) => <StatusBadge value={h.mode} />,
    },
    {
      key: 'goal_health',
      header: 'Goal Health',
      render: (h) => (
        <span className="mono text-xs">
          {h.goal_health != null ? h.goal_health.toFixed(2) : '—'}
        </span>
      ),
    },
    {
      key: 'errors',
      header: 'Errors',
      render: (h) => (
        <span
          className="mono text-xs"
          style={{
            color: h.error_count > 0 ? 'var(--color-destructive)' : undefined,
          }}
        >
          {h.error_count}
        </span>
      ),
    },
    {
      key: 'agents',
      header: 'Agents',
      render: (h) => (
        <span className="text-xs text-[var(--color-muted-foreground)]">
          {h.enabled_agents.length}
        </span>
      ),
    },
  ]

  return (
    <>
      <PageHeader
        title={slug}
        description="Per-repository fleet detail with recent heartbeat history."
        actions={
          <Link
            to="/fleet"
            className="text-xs underline hover:opacity-80 text-[var(--color-muted-foreground)]"
          >
            ← Back to fleet
          </Link>
        }
      />
      <div className="p-6 space-y-6">
        {error ? (
          <p className="text-sm" style={{ color: 'var(--color-destructive)' }}>
            {(error as Error)?.message?.includes('404')
              ? 'This repository has not registered with the fleet yet.'
              : 'Failed to load detail.'}
          </p>
        ) : isLoading || !data ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
              <StatPanel
                label="Caretaker version"
                value={data.caretaker_version}
                hint={`First seen ${timeAgo(data.first_seen)}`}
                accentVar="--chart-1"
              />
              <StatPanel
                label="Last heartbeat"
                value={timeAgo(data.last_seen)}
                hint={data.last_seen}
                accentVar="--chart-2"
              />
              <StatPanel
                label="Last goal health"
                value={
                  data.last_goal_health != null
                    ? data.last_goal_health.toFixed(2)
                    : '—'
                }
                hint={`${data.last_error_count} errors in last run`}
                accentVar="--chart-3"
              />
              <StatPanel
                label="Total heartbeats"
                value={data.heartbeats_seen}
                hint={`Last mode: ${data.last_mode}`}
                accentVar="--chart-4"
              />
            </div>

            <div className="panel p-4 space-y-2">
              <h3 className="text-xs uppercase tracking-[0.08em] text-[var(--color-muted-foreground)]">
                Enabled agents
              </h3>
              <div className="flex flex-wrap gap-2">
                {data.enabled_agents.length === 0 ? (
                  <span className="text-xs text-[var(--color-muted-foreground)]">
                    None reported
                  </span>
                ) : (
                  data.enabled_agents.map((agent) => (
                    <span
                      key={agent}
                      className="mono text-xs px-2 py-0.5 rounded border"
                      style={{
                        borderColor: 'var(--color-border)',
                        background: 'var(--color-muted)',
                      }}
                    >
                      {agent}
                    </span>
                  ))
                )}
              </div>
            </div>

            <div className="panel p-4 space-y-3">
              <h3 className="text-sm font-medium">
                Recent heartbeats ({orderedHistory.length})
              </h3>
              <DataTable
                columns={columns}
                rows={orderedHistory}
                empty="No heartbeat history yet."
              />
            </div>

            {data.last_summary ? (
              <div className="panel p-4 space-y-3">
                <h3 className="text-sm font-medium">Last summary payload</h3>
                <JsonViewer data={data.last_summary} />
              </div>
            ) : null}
          </>
        )}
      </div>
    </>
  )
}
