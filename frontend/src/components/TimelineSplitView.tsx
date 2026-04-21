import { useState } from 'react'
import useSWR from 'swr'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceLine,
} from 'recharts'
import Graph2DView from '@/components/Graph2DView'
import type { Paginated, RunSummary, SubGraph } from '@/lib/types'

type RunPoint = { run_at: string; goal_health: number | null; mode: string; errors: number }

function toRunPoints(runs: RunSummary[]): RunPoint[] {
  return [...runs]
    .sort((a, b) => a.run_at.localeCompare(b.run_at))
    .map((r) => ({
      run_at: r.run_at,
      goal_health: r.goal_health,
      mode: r.mode,
      errors: r.errors?.length ?? 0,
    }))
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function RunTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  const pt = payload[0]?.payload as RunPoint
  return (
    <div
      className="text-xs rounded border border-[var(--color-border)] p-2 space-y-0.5"
      style={{
        background: 'var(--color-card-elevated)',
        color: 'var(--color-foreground)',
        boxShadow: 'var(--shadow-md)',
      }}
    >
      <div className="font-mono">{new Date(label as string).toLocaleString()}</div>
      <div>Mode: {pt.mode}</div>
      <div>Goal health: {pt.goal_health != null ? pt.goal_health.toFixed(3) : '—'}</div>
      {pt.errors > 0 && (
        <div style={{ color: 'var(--color-destructive)' }}>Errors: {pt.errors}</div>
      )}
    </div>
  )
}

export default function TimelineSplitView() {
  const [selectedRunAt, setSelectedRunAt] = useState<string | null>(null)
  const [depth, setDepth] = useState(1)

  const { data: runsPage } = useSWR<Paginated<RunSummary>>('/api/admin/runs?limit=100')
  const runs = runsPage?.items ?? []
  const points = toRunPoints(runs)

  const nodeId = selectedRunAt ? `run:${selectedRunAt}` : null
  const { data: subgraph, isLoading: sgLoading } = useSWR<SubGraph>(
    nodeId ? `/api/graph/neighbors/${encodeURIComponent(nodeId)}?depth=${depth}` : null,
  )

  const selectedRun = runs.find((r) => r.run_at === selectedRunAt)

  return (
    <div className="flex h-full">
      {/* Left — timeline */}
      <div className="w-[420px] shrink-0 flex flex-col border-r border-[var(--color-border)]">
        <div className="p-4 border-b border-[var(--color-border)]">
          <h3 className="text-sm font-semibold">Run timeline</h3>
          <p className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
            Click a data point to load its graph neighborhood.
          </p>
        </div>
        <div className="flex-1 p-4 overflow-y-auto">
          {points.length === 0 ? (
            <p className="text-sm text-[var(--color-muted-foreground)]">No runs recorded yet.</p>
          ) : (
            <>
              <div className="h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={points}
                    margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
                    onClick={(e: unknown) => {
                      const chart = e as { activePayload?: { payload: RunPoint }[] } | null
                      const pt = chart?.activePayload?.[0]?.payload
                      if (pt) setSelectedRunAt(pt.run_at)
                    }}
                    style={{ cursor: 'pointer' }}
                  >
                    <CartesianGrid
                      strokeDasharray="2 4"
                      stroke="var(--color-border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="run_at"
                      tickFormatter={(v) =>
                        new Date(v as string).toLocaleDateString(undefined, {
                          month: 'short',
                          day: 'numeric',
                        })
                      }
                      tick={{ fontSize: 9, fill: 'var(--color-muted-foreground)' }}
                      stroke="var(--color-border)"
                      tickLine={false}
                      axisLine={false}
                    />
                    <YAxis
                      domain={[0, 1]}
                      tick={{ fontSize: 9, fill: 'var(--color-muted-foreground)' }}
                      stroke="var(--color-border)"
                      tickLine={false}
                      axisLine={false}
                      width={28}
                    />
                    <Tooltip content={<RunTooltip />} />
                    {selectedRunAt && (
                      <ReferenceLine
                        x={selectedRunAt}
                        stroke="var(--color-primary)"
                        strokeWidth={1.5}
                        strokeDasharray="3 3"
                      />
                    )}
                    <Line
                      type="monotone"
                      dataKey="goal_health"
                      stroke="var(--chart-1)"
                      strokeWidth={2}
                      dot={{ r: 3, fill: 'var(--chart-1)', strokeWidth: 0 }}
                      activeDot={{ r: 5 }}
                      isAnimationActive={false}
                      connectNulls={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>

              <div className="mt-4 space-y-1">
                {[...points].reverse().slice(0, 30).map((pt) => (
                  <button
                    key={pt.run_at}
                    onClick={() => setSelectedRunAt(pt.run_at)}
                    className="w-full text-left flex items-center justify-between px-2 py-1.5 rounded text-xs hover:bg-[var(--color-muted)] transition-colors"
                    style={{
                      background:
                        pt.run_at === selectedRunAt ? 'var(--color-muted)' : undefined,
                      fontWeight: pt.run_at === selectedRunAt ? 600 : undefined,
                    }}
                  >
                    <span className="font-mono text-[var(--color-muted-foreground)]">
                      {new Date(pt.run_at).toLocaleString(undefined, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                    <span className="flex items-center gap-2">
                      <span className="text-[var(--color-muted-foreground)]">{pt.mode}</span>
                      {pt.goal_health != null && (
                        <span
                          style={{
                            color:
                              pt.goal_health >= 0.7
                                ? 'var(--color-success)'
                                : pt.goal_health >= 0.4
                                  ? 'var(--color-warning)'
                                  : 'var(--color-destructive)',
                          }}
                        >
                          {pt.goal_health.toFixed(2)}
                        </span>
                      )}
                      {pt.errors > 0 && (
                        <span style={{ color: 'var(--color-destructive)' }}>
                          {pt.errors}e
                        </span>
                      )}
                    </span>
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* Right — subgraph */}
      <div className="flex-1 flex flex-col">
        <div className="p-4 border-b border-[var(--color-border)] flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold">
              {selectedRun
                ? `Run — ${new Date(selectedRun.run_at).toLocaleString()}`
                : 'Select a run to explore'}
            </h3>
            {selectedRun && (
              <p className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
                Mode: {selectedRun.mode} · Goal health:{' '}
                {selectedRun.goal_health != null ? selectedRun.goal_health.toFixed(3) : '—'}
              </p>
            )}
          </div>
          {selectedRunAt && (
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
              Depth
              <select
                value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
                className="border border-[var(--color-border)] rounded px-1 py-0.5 text-xs bg-transparent"
              >
                <option value={1}>1</option>
                <option value={2}>2</option>
                <option value={3}>3</option>
              </select>
            </label>
          )}
        </div>
        <div className="flex-1 relative">
          {!selectedRunAt ? (
            <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
              ← Select a run from the timeline
            </div>
          ) : sgLoading ? (
            <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
              Loading neighborhood…
            </div>
          ) : !subgraph || subgraph.nodes.length === 0 ? (
            <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
              No graph data for this run (Neo4j not yet populated?)
            </div>
          ) : (
            <Graph2DView subgraph={subgraph} />
          )}
        </div>
      </div>
    </div>
  )
}
