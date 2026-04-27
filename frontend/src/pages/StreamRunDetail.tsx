import { useMemo, useRef, useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'
import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import { useRunStream } from '@/hooks/useRunStream'
import type { StreamRun } from './StreamRuns'

const STREAM_COLORS: Record<string, string> = {
  stdout: 'text-zinc-200',
  stderr: 'text-red-300',
  system: 'text-blue-300',
  event: 'text-green-300',
}

export default function StreamRunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const { data: run } = useSWR<StreamRun>(
    runId ? `/api/admin/runs/${encodeURIComponent(runId)}` : null,
    { refreshInterval: 5_000 },
  )
  const { events, status, ended, errors } = useRunStream(runId ?? null)
  const tailRef = useRef<HTMLDivElement | null>(null)

  // Stick to bottom when at bottom (auto-tail).
  useEffect(() => {
    const el = tailRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (atBottom) el.scrollTop = el.scrollHeight
  }, [events])

  const lastSeq = useMemo(() => {
    let max = 0
    for (const e of events) {
      if (e.kind === 'log' && e.seq > max) max = e.seq
    }
    return max
  }, [events])

  return (
    <div className="p-4 space-y-3 h-screen flex flex-col">
      <PageHeader
        title={`Run ${runId?.slice(0, 8) ?? ''}…`}
        description={run ? `${run.repository} · ${run.mode}` : 'Loading…'}
        actions={
          <Link to="/streams" className="px-3 py-1 text-xs rounded border">
            Back
          </Link>
        }
      />

      <div className="flex gap-2 text-xs text-zinc-600">
        <span>
          Stream: <span className="font-mono">{status}</span>
        </span>
        <span>
          Run status: <span className="font-mono">{run?.status ?? '—'}</span>
        </span>
        <span>
          last seq: <span className="font-mono tabular-nums">{lastSeq}</span>
        </span>
        {run?.exit_code !== null && run?.exit_code !== undefined ? (
          <span>
            exit_code: <span className="font-mono tabular-nums">{run.exit_code}</span>
          </span>
        ) : null}
      </div>

      {errors.length ? (
        <div className="text-xs text-amber-600">
          {errors.length} connection issue{errors.length === 1 ? '' : 's'}; auto-reconnecting…
        </div>
      ) : null}

      <div
        ref={tailRef}
        className="flex-1 overflow-auto bg-zinc-900 text-zinc-200 font-mono text-xs p-3 rounded"
      >
        {events.map((e, idx) => {
          if (e.kind === 'log') {
            return (
              <div key={`${e.seq}-${idx}`} className={STREAM_COLORS[e.stream] ?? 'text-zinc-200'}>
                <span className="opacity-50 mr-2 tabular-nums">
                  {String(e.seq).padStart(4, ' ')}
                </span>
                {e.data}
              </div>
            )
          }
          if (e.kind === 'end') {
            return (
              <div key={`end-${idx}`} className="text-amber-300 font-bold mt-2">
                ── run finished ({e.status}) ──
              </div>
            )
          }
          return (
            <div key={`gap-${idx}`} className="text-amber-400">
              [gap] {e.reason}
            </div>
          )
        })}
        {events.length === 0 && !ended ? (
          <div className="text-zinc-500">Waiting for log entries…</div>
        ) : null}
      </div>
    </div>
  )
}
