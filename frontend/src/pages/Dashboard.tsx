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
} from 'recharts'
import PageHeader from '@/components/PageHeader'
import type {
  Paginated,
  RunSummary,
  TrackedPR,
  TrackedIssue,
  GoalSnapshot,
} from '@/lib/types'

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="border border-[var(--color-border)] rounded-lg p-4 bg-[var(--color-card)]">
      <p className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {label}
      </p>
      <p className="text-2xl font-semibold mt-1">{value}</p>
    </div>
  )
}

export default function Dashboard() {
  const { data: latestRun } = useSWR<RunSummary>('/api/admin/runs/latest')
  const { data: prs } = useSWR<Paginated<TrackedPR>>('/api/admin/prs?limit=1')
  const { data: issues } = useSWR<Paginated<TrackedIssue>>('/api/admin/issues?limit=1')
  const { data: goals } = useSWR<Record<string, GoalSnapshot[]>>('/api/admin/goals')

  const goalIds = goals ? Object.keys(goals) : []
  const firstGoal = goalIds[0]
  const series = firstGoal && goals ? goals[firstGoal].slice(-30) : []

  return (
    <>
      <PageHeader
        title="Overview"
        description="Real-time snapshot of the caretaker orchestrator."
      />
      <div className="p-8 space-y-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <Stat label="Tracked PRs" value={prs?.total ?? '—'} />
          <Stat label="Tracked issues" value={issues?.total ?? '—'} />
          <Stat
            label="Last run merged"
            value={latestRun?.prs_merged ?? '—'}
          />
          <Stat
            label="Goal health"
            value={
              latestRun?.goal_health != null
                ? latestRun.goal_health.toFixed(2)
                : '—'
            }
          />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="border border-[var(--color-border)] rounded-lg p-5 bg-[var(--color-card)]">
            <h2 className="text-sm font-medium mb-3">Latest run</h2>
            {latestRun ? (
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="text-[var(--color-muted-foreground)]">Ran at</dt>
                <dd>{new Date(latestRun.run_at).toLocaleString()}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Mode</dt>
                <dd>{latestRun.mode}</dd>
                <dt className="text-[var(--color-muted-foreground)]">PRs monitored</dt>
                <dd>{latestRun.prs_monitored}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Issues triaged</dt>
                <dd>{latestRun.issues_triaged}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Escalations</dt>
                <dd>{latestRun.prs_escalated + latestRun.issues_escalated}</dd>
                <dt className="text-[var(--color-muted-foreground)]">Errors</dt>
                <dd>{latestRun.errors?.length ?? 0}</dd>
              </dl>
            ) : (
              <p className="text-sm text-[var(--color-muted-foreground)]">
                No runs recorded yet.
              </p>
            )}
            <Link
              to="/runs"
              className="mt-4 inline-block text-xs text-[var(--color-primary)] hover:underline"
            >
              View run history →
            </Link>
          </div>

          <div className="border border-[var(--color-border)] rounded-lg p-5 bg-[var(--color-card)]">
            <h2 className="text-sm font-medium">
              Goal: <span className="font-normal">{firstGoal ?? '—'}</span>
            </h2>
            {series.length > 0 ? (
              <div className="h-48 mt-3">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={series}>
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="var(--color-border)"
                    />
                    <XAxis
                      dataKey="timestamp"
                      tickFormatter={(v) =>
                        new Date(v as string).toLocaleDateString()
                      }
                      tick={{ fontSize: 10 }}
                    />
                    <YAxis domain={[0, 1]} tick={{ fontSize: 10 }} />
                    <Tooltip
                      labelFormatter={(v) =>
                        new Date(v as string).toLocaleString()
                      }
                    />
                    <Line
                      type="monotone"
                      dataKey="score"
                      stroke="var(--color-primary)"
                      strokeWidth={2}
                      dot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <p className="text-sm text-[var(--color-muted-foreground)] mt-2">
                No goal history yet.
              </p>
            )}
          </div>
        </div>
      </div>
    </>
  )
}
