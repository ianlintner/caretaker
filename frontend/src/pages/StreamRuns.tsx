import { Link } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'

export type StreamRun = {
  run_id: string
  repository: string
  actor: string
  event_name: string
  mode: string
  status: string
  started_at: string
  finished_at: string | null
  exit_code: number | null
  last_seq: number
  last_heartbeat_at: string | null
  workflow: string
  sha: string
}

const STATUS_COLORS: Record<string, string> = {
  pending: 'bg-yellow-200 text-yellow-900',
  running: 'bg-blue-200 text-blue-900',
  succeeded: 'bg-green-200 text-green-900',
  failed: 'bg-red-200 text-red-900',
  stalled: 'bg-orange-200 text-orange-900',
  cancelled: 'bg-zinc-200 text-zinc-900',
}

export default function StreamRuns() {
  const { data, error, isLoading, mutate } = useSWR<StreamRun[]>(
    '/api/admin/runs?limit=100',
    { refreshInterval: 5_000 },
  )

  return (
    <div className="p-4 space-y-4">
      <PageHeader
        title="Streamed Runs"
        description="Live runs streaming from consumer workflows. Click a row to tail logs."
        actions={
          <button
            type="button"
            className="px-3 py-1 text-xs rounded border"
            onClick={() => mutate()}
          >
            Refresh
          </button>
        }
      />
      {error ? (
        <div className="text-sm text-red-600">Failed to load: {String(error)}</div>
      ) : null}
      {isLoading ? <div className="text-sm text-zinc-500">Loading…</div> : null}
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="text-left border-b text-xs uppercase tracking-wide text-zinc-500">
              <th className="py-2 pr-4">Started</th>
              <th className="py-2 pr-4">Repository</th>
              <th className="py-2 pr-4">Mode</th>
              <th className="py-2 pr-4">Status</th>
              <th className="py-2 pr-4">Actor</th>
              <th className="py-2 pr-4">Event</th>
              <th className="py-2 pr-4">Logs</th>
            </tr>
          </thead>
          <tbody>
            {(data ?? []).map((row) => (
              <tr key={row.run_id} className="border-b hover:bg-zinc-50">
                <td className="py-2 pr-4 text-xs tabular-nums">
                  {new Date(row.started_at).toLocaleString()}
                </td>
                <td className="py-2 pr-4">
                  <Link
                    to={`/streams/${encodeURIComponent(row.run_id)}`}
                    className="font-mono text-xs underline-offset-2 hover:underline"
                  >
                    {row.repository}
                  </Link>
                </td>
                <td className="py-2 pr-4 text-xs">{row.mode}</td>
                <td className="py-2 pr-4">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[row.status] ?? 'bg-zinc-200'}`}
                  >
                    {row.status}
                  </span>
                </td>
                <td className="py-2 pr-4 text-xs">{row.actor}</td>
                <td className="py-2 pr-4 text-xs">{row.event_name}</td>
                <td className="py-2 pr-4 tabular-nums text-xs">{row.last_seq}</td>
              </tr>
            ))}
            {data && data.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-6 text-center text-sm text-zinc-500">
                  No streamed runs recorded yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  )
}
