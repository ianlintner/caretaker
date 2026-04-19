import { lazy, Suspense, useState } from 'react'
import useSWR from 'swr'
import { Boxes, Network } from 'lucide-react'
import PageHeader from '@/components/PageHeader'
import Graph2DView from '@/components/Graph2DView'
import { useSize } from '@/hooks/useSize'

const Graph3DView = lazy(() => import('@/components/Graph3DView'))
import { cn } from '@/lib/cn'
import type { GraphStats, SubGraph } from '@/lib/types'

const NODE_COLORS: Record<string, string> = {
  Agent: '#7c3aed',
  PR: '#059669',
  Issue: '#dc2626',
  Goal: '#d97706',
  Skill: '#2563eb',
  Run: '#6b7280',
  AuditEvent: '#a855f7',
  Mutation: '#db2777',
}

const NODE_TYPES = [
  'Agent',
  'PR',
  'Issue',
  'Goal',
  'Skill',
  'Run',
  'AuditEvent',
  'Mutation',
]

type View = '2d' | '3d'

export default function Graph() {
  const [view, setView] = useState<View>('2d')
  const [selected, setSelected] = useState<Set<string>>(
    new Set(['Agent', 'PR', 'Issue', 'Goal']),
  )
  const { ref, width, height } = useSize<HTMLDivElement>()

  const typesParam = Array.from(selected).join(',')
  const { data: stats } = useSWR<GraphStats>('/api/graph/stats')
  const { data: subgraph, isLoading, error } = useSWR<SubGraph>(
    typesParam
      ? `/api/graph/subgraph?types=${typesParam}&limit=200`
      : '/api/graph/subgraph?limit=200',
  )

  function toggle(type: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })
  }

  return (
    <>
      <PageHeader
        title="Knowledge graph"
        description={
          stats
            ? `${stats.total_nodes} nodes, ${stats.total_edges} edges`
            : 'Neo4j-backed relationship graph.'
        }
        actions={
          <div className="inline-flex rounded-md border border-[var(--color-border)] overflow-hidden">
            <button
              onClick={() => setView('2d')}
              className={cn(
                'flex items-center gap-1.5 px-3 py-1.5 text-xs',
                view === '2d'
                  ? 'bg-[var(--color-primary)] text-[var(--color-primary-foreground)]'
                  : 'hover:bg-[var(--color-muted)]',
              )}
            >
              <Network className="h-3.5 w-3.5" />
              2D
            </button>
            <button
              onClick={() => setView('3d')}
              className={cn(
                'flex items-center gap-1.5 px-3 py-1.5 text-xs border-l border-[var(--color-border)]',
                view === '3d'
                  ? 'bg-[var(--color-primary)] text-[var(--color-primary-foreground)]'
                  : 'hover:bg-[var(--color-muted)]',
              )}
            >
              <Boxes className="h-3.5 w-3.5" />
              3D
            </button>
          </div>
        }
      />
      <div className="flex h-[calc(100vh-98px)]">
        <aside className="w-60 shrink-0 border-r border-[var(--color-border)] p-4 overflow-y-auto">
          <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mb-2">
            Filter types
          </h3>
          <div className="space-y-1.5">
            {NODE_TYPES.map((type) => {
              const count = stats?.node_counts[type] ?? 0
              const color = NODE_COLORS[type] || '#a1a1aa'
              return (
                <label
                  key={type}
                  className="flex items-center gap-2 cursor-pointer text-sm"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(type)}
                    onChange={() => toggle(type)}
                  />
                  <span
                    className="inline-block w-3 h-3 rounded-full"
                    style={{ background: color }}
                  />
                  <span className="flex-1">{type}</span>
                  <span className="text-xs text-[var(--color-muted-foreground)] tabular-nums">
                    {count}
                  </span>
                </label>
              )
            })}
          </div>

          {stats && Object.keys(stats.edge_counts).length > 0 && (
            <>
              <h3 className="text-xs uppercase tracking-wide text-[var(--color-muted-foreground)] mt-5 mb-2">
                Relationships
              </h3>
              <div className="space-y-1 text-xs">
                {Object.entries(stats.edge_counts).map(([rel, cnt]) => (
                  <div key={rel} className="flex justify-between">
                    <span className="font-mono">{rel}</span>
                    <span className="text-[var(--color-muted-foreground)] tabular-nums">
                      {cnt}
                    </span>
                  </div>
                ))}
              </div>
            </>
          )}
        </aside>
        <div ref={ref} className="flex-1 relative">
          {error ? (
            <div className="p-8 text-sm text-[var(--color-muted-foreground)]">
              Graph API unavailable. Is Neo4j configured?
            </div>
          ) : isLoading ? (
            <div className="p-8 text-sm text-[var(--color-muted-foreground)]">
              Loading graph…
            </div>
          ) : subgraph && subgraph.nodes.length === 0 ? (
            <div className="p-8 text-sm text-[var(--color-muted-foreground)]">
              No nodes for the selected filters.
            </div>
          ) : subgraph ? (
            view === '2d' ? (
              <Graph2DView subgraph={subgraph} />
            ) : width > 0 && height > 0 ? (
              <Suspense
                fallback={
                  <div className="p-8 text-sm text-[var(--color-muted-foreground)]">
                    Loading 3D view…
                  </div>
                }
              >
                <Graph3DView subgraph={subgraph} width={width} height={height} />
              </Suspense>
            ) : null
          ) : null}
        </div>
      </div>
    </>
  )
}
