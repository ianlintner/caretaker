import { useState } from 'react'
import useSWR from 'swr'
import { GitBranch, ArrowRight } from 'lucide-react'
import type { CausalEvent, CausalChainResult, CausalDescendantsResult, Paginated } from '@/lib/types'

function EventRow({
  event,
  selected,
  onClick,
  indent = 0,
}: {
  event: CausalEvent
  selected: boolean
  onClick: () => void
  indent?: number
}) {
  return (
    <button
      onClick={onClick}
      className="w-full text-left px-2 py-1.5 rounded text-xs hover:bg-[var(--color-muted)] transition-colors flex items-start gap-2"
      style={{
        paddingLeft: `${8 + indent * 16}px`,
        background: selected ? 'var(--color-muted)' : undefined,
        fontWeight: selected ? 600 : undefined,
      }}
    >
      <span className="mt-0.5 shrink-0 text-[var(--color-muted-foreground)]">
        <GitBranch className="h-3 w-3" />
      </span>
      <span className="flex-1 min-w-0">
        <span className="font-mono block truncate text-[var(--color-foreground)]">
          {event.id}
        </span>
        {event.title && (
          <span className="text-[var(--color-muted-foreground)] truncate block">{event.title}</span>
        )}
        <span className="text-[var(--color-muted-foreground)] truncate block">
          {event.source}
          {event.observed_at && (
            <> · {new Date(event.observed_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}</>
          )}
        </span>
      </span>
    </button>
  )
}

function ChainPanel({ eventId }: { eventId: string }) {
  const { data: chainData } = useSWR<CausalChainResult>(`/api/admin/causal/${encodeURIComponent(eventId)}`)
  const { data: descData } = useSWR<CausalDescendantsResult>(
    `/api/admin/causal/${encodeURIComponent(eventId)}/descendants`,
  )

  const chain = chainData?.events ?? []
  const descendants = descData?.events ?? []

  return (
    <div className="flex h-full divide-x divide-[var(--color-border)]">
      {/* Ancestor chain */}
      <div className="w-1/2 flex flex-col">
        <div className="p-3 border-b border-[var(--color-border)]">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
            Ancestor chain ({chain.length})
          </h4>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {chain.length === 0 ? (
            <p className="text-xs text-[var(--color-muted-foreground)] p-2">No ancestor chain.</p>
          ) : (
            chain.map((e, i) => (
              <div key={e.id} className="flex items-start gap-1.5">
                {i > 0 && (
                  <span className="mt-1.5 ml-2 text-[var(--color-muted-foreground)]">
                    <ArrowRight className="h-3 w-3 rotate-90" />
                  </span>
                )}
                <div
                  className="flex-1 rounded border border-[var(--color-border)] px-2 py-1 text-xs"
                  style={{
                    background: e.id === eventId ? 'var(--color-muted)' : undefined,
                    fontWeight: e.id === eventId ? 600 : undefined,
                  }}
                >
                  <div className="font-mono truncate">{e.id}</div>
                  {e.title && <div className="text-[var(--color-muted-foreground)] truncate">{e.title}</div>}
                  <div className="text-[var(--color-muted-foreground)] truncate">{e.source}</div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Descendants */}
      <div className="w-1/2 flex flex-col">
        <div className="p-3 border-b border-[var(--color-border)]">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
            Descendants ({descendants.length})
          </h4>
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {descendants.length === 0 ? (
            <p className="text-xs text-[var(--color-muted-foreground)] p-2">No descendants.</p>
          ) : (
            descendants.map((e) => (
              <div
                key={e.id}
                className="rounded border border-[var(--color-border)] px-2 py-1 text-xs"
              >
                <div className="font-mono truncate">{e.id}</div>
                {e.title && (
                  <div className="text-[var(--color-muted-foreground)] truncate">{e.title}</div>
                )}
                <div className="text-[var(--color-muted-foreground)] truncate">
                  {e.source}
                  {e.observed_at && (
                    <> · {new Date(e.observed_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}</>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}

export default function CausalChainView() {
  const [source, setSource] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const qs = source ? `?source=${encodeURIComponent(source)}&limit=50` : '?limit=50'
  const { data: eventsPage } = useSWR<Paginated<CausalEvent>>(`/api/admin/causal${qs}`)
  const events = eventsPage?.items ?? []

  return (
    <div className="flex h-full">
      {/* Left — event list */}
      <div className="w-[320px] shrink-0 flex flex-col border-r border-[var(--color-border)]">
        <div className="p-3 border-b border-[var(--color-border)] space-y-2">
          <h3 className="text-sm font-semibold">Causal events</h3>
          <input
            type="text"
            placeholder="Filter by source…"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            className="w-full border border-[var(--color-border)] rounded px-2 py-1 text-xs bg-transparent placeholder:text-[var(--color-muted-foreground)]"
          />
        </div>
        <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {events.length === 0 ? (
            <p className="text-xs text-[var(--color-muted-foreground)] p-2">
              No causal events recorded.
            </p>
          ) : (
            events.map((e) => (
              <EventRow
                key={e.id}
                event={e}
                selected={e.id === selectedId}
                onClick={() => setSelectedId(e.id)}
              />
            ))
          )}
        </div>
        {eventsPage && eventsPage.total > eventsPage.items.length && (
          <div className="p-2 text-xs text-center text-[var(--color-muted-foreground)] border-t border-[var(--color-border)]">
            Showing {eventsPage.items.length} of {eventsPage.total}
          </div>
        )}
      </div>

      {/* Right — chain detail */}
      <div className="flex-1 overflow-hidden">
        {selectedId ? (
          <ChainPanel eventId={selectedId} />
        ) : (
          <div className="h-full flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
            ← Select an event to explore its chain
          </div>
        )}
      </div>
    </div>
  )
}
