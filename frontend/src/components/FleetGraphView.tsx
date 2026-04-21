import { useMemo } from 'react'
import useSWR from 'swr'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node as FlowNode,
  type Edge as FlowEdge,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { SubGraph } from '@/lib/types'

type FleetClient = {
  repo: string
  last_goal_health: number | null
  last_error_count: number
  caretaker_version: string
  last_seen: string
  enabled_agents: string[]
}

type FleetList = { items: FleetClient[]; total: number; offset: number; limit: number }

function timeAgo(iso: string): string {
  const delta = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(delta)) return '—'
  if (delta < 60_000) return 'just now'
  const m = Math.floor(delta / 60_000)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function healthColor(score: number | null): string {
  if (score == null) return '#94a3b8'
  if (score >= 0.7) return '#059669'
  if (score >= 0.4) return '#d97706'
  return '#dc2626'
}

function buildFleetGraph(
  clients: FleetClient[],
  graphSubgraph: SubGraph | undefined,
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const repoClientMap = new Map(clients.map((c) => [c.repo, c]))

  // Repo nodes — position in a circle
  const repoNodes: FlowNode[] = clients.map((c, i) => {
    const angle = (2 * Math.PI * i) / Math.max(clients.length, 1)
    const radius = Math.max(200, clients.length * 50)
    const x = Math.round(radius * Math.cos(angle))
    const y = Math.round(radius * Math.sin(angle))
    const health = c.last_goal_health
    const color = healthColor(health)
    const size = health != null ? 20 + health * 30 : 24
    return {
      id: `repo:${c.repo}`,
      position: { x, y },
      data: {
        label: `${c.repo}\n${health != null ? health.toFixed(2) : '?'}`,
      },
      style: {
        background: `${color}22`,
        border: `2px solid ${color}`,
        color: '#1f2937',
        borderRadius: 8,
        fontSize: 10,
        padding: '6px 10px',
        width: size * 3,
        whiteSpace: 'pre-line',
        textAlign: 'center',
      },
    }
  })

  // GlobalSkill nodes from graph subgraph
  const skillNodes: FlowNode[] = []
  const skillEdges: FlowEdge[] = []

  if (graphSubgraph) {
    const gsNodes = graphSubgraph.nodes.filter((n) => n.type === 'GlobalSkill')
    gsNodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / Math.max(gsNodes.length, 1)
      const radius = 80
      skillNodes.push({
        id: n.id,
        position: { x: Math.round(radius * Math.cos(angle)), y: Math.round(radius * Math.sin(angle)) },
        data: { label: n.label },
        style: {
          background: '#eff6ff',
          border: '1px solid #3b82f6',
          color: '#1e3a8a',
          borderRadius: 4,
          fontSize: 9,
          padding: '4px 8px',
        },
      })
    })

    graphSubgraph.edges.forEach((e) => {
      const src = e.source
      const tgt = e.target
      if (e.type === 'SHARES_SKILL' || e.type === 'PROMOTED_TO') {
        // Map repo node or skill node
        const sourceId = repoClientMap.has(src) ? `repo:${src}` : src
        skillEdges.push({
          id: e.id,
          source: sourceId,
          target: tgt,
          label: e.type,
          labelStyle: { fontSize: 8, fill: '#6b7280' },
          style: { stroke: '#94a3b8', strokeWidth: 1, strokeDasharray: '4 2' },
        })
      }
    })
  }

  return { nodes: [...repoNodes, ...skillNodes], edges: skillEdges }
}

export default function FleetGraphView() {
  const { data: fleet } = useSWR<FleetList>('/api/admin/fleet?limit=100')
  const { data: graphSubgraph } = useSWR<SubGraph>(
    '/api/graph/subgraph?types=Repo,GlobalSkill&limit=500',
  )

  const clients = fleet?.items ?? []
  const { nodes, edges } = useMemo(
    () => buildFleetGraph(clients, graphSubgraph),
    [clients, graphSubgraph],
  )

  if (clients.length === 0) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center space-y-2">
          <p className="text-sm text-[var(--color-muted-foreground)]">No fleet clients registered.</p>
          <p className="text-xs text-[var(--color-muted-foreground)]">
            Set <code className="font-mono">fleet_registry.enabled = true</code> in consumer repos.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full">
      {/* Graph */}
      <div className="flex-1 relative">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          fitViewOptions={{ padding: 0.3 }}
          minZoom={0.1}
        >
          <Background gap={16} />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>

      {/* Sidebar — repo list */}
      <div className="w-72 shrink-0 border-l border-[var(--color-border)] flex flex-col">
        <div className="p-3 border-b border-[var(--color-border)]">
          <h3 className="text-sm font-semibold">Fleet clients</h3>
          <p className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
            {clients.length} repo{clients.length !== 1 ? 's' : ''}
          </p>
        </div>
        <div className="flex-1 overflow-y-auto divide-y divide-[var(--color-border)]">
          {clients.map((c) => (
            <div key={c.repo} className="p-3 text-xs space-y-1">
              <div className="font-mono font-semibold truncate">{c.repo}</div>
              <div className="flex items-center gap-3">
                <span
                  className="font-mono font-semibold"
                  style={{ color: healthColor(c.last_goal_health) }}
                >
                  {c.last_goal_health != null ? c.last_goal_health.toFixed(2) : '—'}
                </span>
                <span className="text-[var(--color-muted-foreground)]">v{c.caretaker_version}</span>
                <span className="text-[var(--color-muted-foreground)]">{timeAgo(c.last_seen)}</span>
              </div>
              {c.last_error_count > 0 && (
                <div style={{ color: 'var(--color-destructive)' }}>
                  {c.last_error_count} error{c.last_error_count !== 1 ? 's' : ''}
                </div>
              )}
              {c.enabled_agents.length > 0 && (
                <div className="text-[var(--color-muted-foreground)] truncate">
                  {c.enabled_agents.join(', ')}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
