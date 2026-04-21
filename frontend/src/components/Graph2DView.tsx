import { useCallback, useMemo, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node as FlowNode,
  type Edge as FlowEdge,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { GraphNode, SubGraph } from '@/lib/types'
import { NODE_COLOR_MAP } from '@/lib/nodeColors'

function layout(subgraph: SubGraph): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const byType = new Map<string, { id: string; label: string }[]>()
  for (const n of subgraph.nodes) {
    const list = byType.get(n.type) ?? []
    list.push({ id: n.id, label: n.label })
    byType.set(n.type, list)
  }

  const positions = new Map<string, { x: number; y: number }>()
  const types = Array.from(byType.keys())
  const BAND_HEIGHT = 160
  const NODE_SPACING = 200

  types.forEach((type, typeIdx) => {
    const nodes = byType.get(type)!
    const y = typeIdx * BAND_HEIGHT
    nodes.forEach((n, i) => {
      positions.set(n.id, { x: i * NODE_SPACING, y })
    })
  })

  const flowNodes: FlowNode[] = subgraph.nodes.map((n) => {
    const color = NODE_COLOR_MAP[n.type] ?? NODE_COLOR_MAP.Unknown
    return {
      id: n.id,
      position: positions.get(n.id) || { x: 0, y: 0 },
      data: { label: `${n.type}: ${n.label}` },
      style: {
        background: color.bg,
        border: `1px solid ${color.border}`,
        color: color.text,
        borderRadius: 6,
        fontSize: 11,
        padding: '6px 10px',
        minWidth: 80,
      },
    }
  })

  const flowEdges: FlowEdge[] = subgraph.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.type,
    labelStyle: { fontSize: 9, fill: '#6b7280' },
    style: { stroke: '#a1a1aa', strokeWidth: 1 },
  }))

  return { nodes: flowNodes, edges: flowEdges }
}

type TooltipState = {
  node: GraphNode
  x: number
  y: number
} | null

function NodeTooltip({ state }: { state: TooltipState }) {
  if (!state) return null
  const entries = Object.entries(state.node.properties).filter(
    ([, v]) => v !== null && v !== undefined && v !== '',
  )
  return (
    <div
      className="fixed z-50 pointer-events-none"
      style={{ left: state.x + 14, top: state.y - 10 }}
    >
      <div
        style={{
          background: '#13131f',
          border: '1px solid rgba(255,255,255,0.12)',
          borderRadius: 8,
          padding: '10px 12px',
          boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
          fontSize: 11,
          maxWidth: 280,
        }}
      >
        <div style={{ fontWeight: 600, color: '#f1f5f9', marginBottom: 6 }}>
          {state.node.type}: {state.node.label}
        </div>
        <div style={{ color: '#64748b', fontSize: 10, marginBottom: 6 }}>
          {state.node.id}
        </div>
        {entries.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {entries.slice(0, 10).map(([k, v]) => {
              const display = typeof v === 'string' && v.length > 50 ? v.slice(0, 50) + '…' : String(v)
              return (
                <div key={k} style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
                  <span style={{ color: '#6b7280', shrink: 0, minWidth: 70 }}>{k}:</span>
                  <span style={{ color: '#d1d5db', wordBreak: 'break-word' }}>{display}</span>
                </div>
              )
            })}
            {entries.length > 10 && (
              <div style={{ color: '#6b7280', marginTop: 2 }}>+{entries.length - 10} more…</div>
            )}
          </div>
        ) : (
          <div style={{ color: '#6b7280' }}>No properties</div>
        )}
      </div>
    </div>
  )
}

export default function Graph2DView({
  subgraph,
  onNodeClick,
}: {
  subgraph: SubGraph
  onNodeClick?: (nodeId: string, nodeType: string) => void
}) {
  const { nodes, edges } = useMemo(() => layout(subgraph), [subgraph])
  const [tooltip, setTooltip] = useState<TooltipState>(null)

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_evt, node) => {
      if (!onNodeClick) return
      const original = subgraph.nodes.find((n) => n.id === node.id)
      onNodeClick(node.id, original?.type ?? 'Unknown')
    },
    [onNodeClick, subgraph.nodes],
  )

  const handleMouseEnter: NodeMouseHandler = useCallback(
    (evt, node) => {
      const original = subgraph.nodes.find((n) => n.id === node.id)
      if (original) {
        setTooltip({ node: original, x: evt.clientX, y: evt.clientY })
      }
    },
    [subgraph.nodes],
  )

  const handleMouseLeave: NodeMouseHandler = useCallback(() => {
    setTooltip(null)
  }, [])

  return (
    <>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.1}
        onNodeClick={onNodeClick ? handleNodeClick : undefined}
        onNodeMouseEnter={handleMouseEnter}
        onNodeMouseLeave={handleMouseLeave}
      >
        <Background gap={16} />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
      <NodeTooltip state={tooltip} />
    </>
  )
}
