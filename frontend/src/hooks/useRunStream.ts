import { useEffect, useRef, useState } from 'react'

export type RunStreamEvent =
  | { kind: 'log'; seq: number; ts: string; stream: string; data: string; tags: Record<string, unknown> }
  | { kind: 'end'; status: string }
  | { kind: 'gap'; reason: string }

type UseRunStreamOptions = {
  /**
   * Maximum number of buffered events kept in state.  Older events fall off
   * the front so the React tree never holds more than this. Default 5_000.
   */
  bufferSize?: number
}

type UseRunStreamResult = {
  events: RunStreamEvent[]
  status: 'connecting' | 'open' | 'closed' | 'error'
  /** True once an `end` event has been received. */
  ended: boolean
  /** Last server-assigned `id:` (== entry seq). Used internally for reconnect. */
  lastEventId: string | null
  /** Pretty errors observed on the connection. */
  errors: string[]
}

/**
 * Subscribe to a run's SSE log stream via the admin endpoint.
 *
 * The browser's built-in EventSource auto-reconnects on transient failures
 * and replays `Last-Event-ID` to the server, which resumes from the next
 * entry by reading Redis Streams from after that seq.
 */
export function useRunStream(runId: string | null, opts: UseRunStreamOptions = {}): UseRunStreamResult {
  const bufferSize = opts.bufferSize ?? 5_000
  const [events, setEvents] = useState<RunStreamEvent[]>([])
  const [status, setStatus] = useState<UseRunStreamResult['status']>('connecting')
  const [ended, setEnded] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [lastEventId, setLastEventId] = useState<string | null>(null)
  const sourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!runId) return
    setEvents([])
    setEnded(false)
    setErrors([])
    setStatus('connecting')

    const url = `/api/admin/runs/${encodeURIComponent(runId)}/stream`
    const es = new EventSource(url, { withCredentials: true })
    sourceRef.current = es

    es.addEventListener('open', () => setStatus('open'))

    const append = (evt: RunStreamEvent, id?: string) => {
      if (id) setLastEventId(id)
      setEvents((prev) => {
        const next = prev.length >= bufferSize ? prev.slice(prev.length - bufferSize + 1) : prev.slice()
        next.push(evt)
        return next
      })
    }

    es.addEventListener('log', (e: MessageEvent) => {
      try {
        const obj = JSON.parse(e.data)
        append(
          {
            kind: 'log',
            seq: Number(obj.seq ?? 0),
            ts: String(obj.ts ?? ''),
            stream: String(obj.stream ?? 'stdout'),
            data: String(obj.data ?? ''),
            tags: (obj.tags as Record<string, unknown>) ?? {},
          },
          (e as MessageEvent).lastEventId,
        )
      } catch {
        append({ kind: 'log', seq: 0, ts: '', stream: 'stdout', data: e.data, tags: {} })
      }
    })

    es.addEventListener('end', (e: MessageEvent) => {
      setEnded(true)
      append({ kind: 'end', status: e.data || 'unknown' })
      setStatus('closed')
      es.close()
    })

    es.addEventListener('gap', (e: MessageEvent) => {
      append({ kind: 'gap', reason: e.data || 'gap' })
    })

    es.addEventListener('error', () => {
      setStatus((prev) => (prev === 'closed' ? prev : 'error'))
      setErrors((prev) => [...prev, 'connection error; reconnecting'])
    })

    return () => {
      es.close()
      sourceRef.current = null
    }
    // bufferSize is intentionally a stable option captured at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  return { events, status, ended, lastEventId, errors }
}
