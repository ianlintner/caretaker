import useSWR from 'swr'
import { Link } from 'react-router-dom'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Area,
} from 'recharts'
import PageHeader from '@/components/PageHeader'
import StatPanel from '@/components/StatPanel'
import type {
  Paginated,
  RunSummary,
  TrackedPR,
  TrackedIssue,
  GoalSnapshot,
} from '@/lib/types'

function trendFromSeries(series: { value: number }[]): 'up' | 'down' | 'flat' | undefined {
  if (series.length < 2) return undefined
  const first = series[0].value
  const last = series[series.length - 1].value
  const diff = last - first
  if (Math.abs(diff) < 0.01) return 'flat'
  return diff > 0 ? 'up' : 'down'
}

function deltaLabel(series: { value: number }[], suffix = ''): string | undefined {
  if (series.length < 2) return undefined
  const diff = series[series.length - 1].value - series[0].value
  const sign = diff > 0 ? '+' : ''
  return `${sign}${diff.toFixed(2)}${suffix}`
}

export default function Dashboard() {
  const { data: latestRun } = useSWR<RunSummary>('/api/admin/runs/latest')
  const { data: prs } = useSWR<Paginated<TrackedPR>>('/api/admin/prs?limit=1')
  const { data: issues } = useSWR<Paginated<TrackedIssue>>('/api/admin/issues?limit=1')
  const { data: goals } = useSWR<Record<string, GoalSnapshot[]>>('/api/admin/goals')

  const goalIds = goals ? Object.keys(goals) : []
  const firstGoal = goalIds[0]
  const series = firstGoal && goals ? goals[firstGoal].slice(-30) : []
  const sparkSeries = series.map((s) => ({ value: s.score }))
  const goalTrend = trendFromSeries(sparkSeries)
  const goalDelta = deltaLabel(sparkSeries)

  const errorCount = latestRun?.errors?.length ?? 0
  const escalations =
    (latestRun?.prs_escalated ?? 0) + (latestRun?.issues_escalated ?? 0)

  return (
    <>
      <PageHeader
        title="Overview"
        description="Real-time snapshot of the caretaker orchestrator."
      />
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatPanel
            label="Tracked PRs"
            value={prs?.total ?? '—'}
            hint="Currently monitored"
            accentVar="--chart-1"
          />
          <StatPanel
            label="Tracked issues"
            value={issues?.total ?? '—'}
            hint="Awaiting triage or in-flight"
            accentVar="--chart-2"
          />
          <StatPanel
            label="Merged last run"
            value={latestRun?.prs_merged ?? '—'}
            hint={
              latestRun
                ? `${latestRun.prs_monitored} PRs monitored`
                : 'No runs recorded yet'
            }
            accentVar="--chart-3"
          />
          <StatPanel
            label="Goal health"
            value={
              latestRun?.goal_health != null
                ? latestRun.goal_health.toFixed(2)
                : '—'
            }
            hint={firstGoal ? `Primary goal: ${firstGoal}` : undefined}
            trend={goalTrend}
            deltaLabel={goalDelta}
            sparkline={sparkSeries}
            accentVar="--chart-1"
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="panel p-5 lg:col-span-1">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold tracking-tight">Latest run</h2>
              <Link
                to="/runs"
                className="text-[11px] text-[var(--color-primary)] hover:underline"
              >
                History →
              </Link>
            </div>
            {latestRun ? (
              <dl className="mt-3 grid grid-cols-2 gap-y-2 text-sm">
                <dt className="text-[var(--color-muted-foreground)]">Ran at</dt>
                <dd className="mono">{new Date(latestRun.run_at).toLocaleString()}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Mode</dt>
                <dd className="mono">{latestRun.mode}</dd>
                <dt className="text-[var(--color-muted-foreground)]">PRs monitored</dt>
                <dd className="mono">{latestRun.prs_monitored}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Issues triaged</dt>
                <dd className="mono">{latestRun.issues_triaged}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Escalations</dt>
                <dd
                  className="mono"
                  style={{
                    color:
                      escalations > 0 ? 'var(--color-warning)' : undefined,
                  }}
                >
                  {escalations}
                </dd>
                <dt className="text-[var(--color-muted-foreground)]">Errors</dt>
                <dd
                  className="mono"
                  style={{
                    color:
                      errorCount > 0 ? 'var(--color-destructive)' : undefined,
                  }}
                >
                  {errorCount}
                </dd>
              </dl>
            ) : (
              <p className="mt-3 text-sm text-[var(--color-muted-foreground)]">
                No runs recorded yet.
              </p>
            )}
          </div>

          <div className="panel p-5 lg:col-span-2">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-sm font-semibold tracking-tight">
                  Goal score
                </h2>
                <p className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
                  {firstGoal ?? 'No goal recorded'}
                </p>
              </div>
              {goalDelta && (
                <span
                  className="mono text-xs font-medium"
                  style={{
                    color:
                      goalTrend === 'up'
                        ? 'var(--color-success)'
                        : goalTrend === 'down'
                          ? 'var(--color-destructive)'
                          : 'var(--color-muted-foreground)',
                  }}
                >
                  {goalDelta}
                </span>
              )}
            </div>
            {series.length > 0 ? (
              <div className="h-56 mt-3">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={series}
                    margin={{ top: 8, right: 8, bottom: 0, left: -12 }}
                  >
                    <defs>
                      <linearGradient id="goal-area" x1="0" x2="0" y1="0" y2="1">
                        <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      strokeDasharray="2 4"
                      stroke="var(--color-border)"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="timestamp"
                      tickFormatter={(v) =>
                        new Date(v as string).toLocaleDateString(undefined, {
                          month: 'short',
                          day: 'numeric',
                        })
                      }
                      tick={{ fontSize: 10, fill: 'var(--color-muted-foreground)' }}
                      stroke="var(--color-border)"
                      tickLine={false}
                      axisLine={false}
                    />
                    <YAxis
                      domain={[0, 1]}
                      tick={{ fontSize: 10, fill: 'var(--color-muted-foreground)' }}
                      stroke="var(--color-border)"
                      tickLine={false}
                      axisLine={false}
                      width={36}
                    />
                    <Tooltip
                      cursor={{ stroke: 'var(--color-border-strong)' }}
                      contentStyle={{
                        background: 'var(--color-card-elevated)',
                        border: '1px solid var(--color-border)',
                        borderRadius: 'var(--radius-sm)',
                        fontSize: 12,
                        color: 'var(--color-foreground)',
                        boxShadow: 'var(--shadow-md)',
                      }}
                      labelFormatter={(v) =>
                        new Date(v as string).toLocaleString()
                      }
                    />
                    <Area
                      type="monotone"
                      dataKey="score"
                      stroke="transparent"
                      fill="url(#goal-area)"
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="score"
                      stroke="var(--chart-1)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="mt-3 text-sm text-[var(--color-muted-foreground)]">
                No goal history yet.
              </p>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
