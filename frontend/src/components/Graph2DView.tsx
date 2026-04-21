import { useCallback, useMemo } from 'react'
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
import type { SubGraph } from '@/lib/types'
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

export default function Graph2DView({
  subgraph,
  onNodeClick,
}: {
  subgraph: SubGraph
  onNodeClick?: (nodeId: string, nodeType: string) => void
}) {
  const { nodes, edges } = useMemo(() => layout(subgraph), [subgraph])

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_evt, node) => {
      if (!onNodeClick) return
      const original = subgraph.nodes.find((n) => n.id === node.id)
      onNodeClick(node.id, original?.type ?? 'Unknown')
    },
    [onNodeClick, subgraph.nodes],
  )

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.1}
      onNodeClick={onNodeClick ? handleNodeClick : undefined}
    >
      <Background gap={16} />
      <Controls />
      <MiniMap pannable zoomable />
    </ReactFlow>
  )
}
