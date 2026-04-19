import { useEffect, useMemo, useRef } from 'react'
import ForceGraph3D, {
  type ForceGraphMethods,
} from 'react-force-graph-3d'
import type { SubGraph } from '@/lib/types'

const NODE_COLORS: Record<string, string> = {
  Agent: '#7c3aed',
  PR: '#059669',
  Issue: '#dc2626',
  Goal: '#d97706',
  Skill: '#2563eb',
  Run: '#6b7280',
  AuditEvent: '#a855f7',
  Mutation: '#db2777',
  Unknown: '#a1a1aa',
}

type FGNode = {
  id: string
  label: string
  type: string
  val: number
}

type FGLink = {
  source: string
  target: string
  type: string
}

export default function Graph3DView({
  subgraph,
  width,
  height,
}: {
  subgraph: SubGraph
  width: number
  height: number
}) {
  const ref = useRef<ForceGraphMethods<FGNode, FGLink>>(undefined)

  const data = useMemo(() => {
    const nodes: FGNode[] = subgraph.nodes.map((n) => ({
      id: n.id,
      label: `${n.type}: ${n.label}`,
      type: n.type,
      val: 1,
    }))
    const links: FGLink[] = subgraph.edges.map((e) => ({
      source: e.source,
      target: e.target,
      type: e.type,
    }))
    return { nodes, links }
  }, [subgraph])

  useEffect(() => {
    const t = setTimeout(() => {
      ref.current?.zoomToFit(400, 40)
    }, 300)
    return () => clearTimeout(t)
  }, [data])

  return (
    <ForceGraph3D
      ref={ref}
      graphData={data}
      width={width}
      height={height}
      backgroundColor="rgba(0,0,0,0)"
      nodeLabel="label"
      nodeColor={(n) => NODE_COLORS[(n as FGNode).type] || NODE_COLORS.Unknown}
      nodeOpacity={0.95}
      nodeResolution={12}
      linkLabel="type"
      linkColor={() => 'rgba(150,150,150,0.5)'}
      linkDirectionalArrowLength={3}
      linkDirectionalArrowRelPos={1}
      linkWidth={0.5}
      enableNodeDrag={true}
      showNavInfo={false}
    />
  )
}
