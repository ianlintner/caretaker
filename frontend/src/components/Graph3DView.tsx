import { useEffect, useMemo, useRef } from 'react'
import ForceGraph3D, {
  type ForceGraphMethods,
} from 'react-force-graph-3d'
import type { SubGraph } from '@/lib/types'
import { NODE_HEX } from '@/lib/nodeColors'

type FGNode = {
  id: string
  label: string
  type: string
  val: number
  properties: Record<string, unknown>
}

type FGLink = {
  source: string
  target: string
  type: string
}

function buildTooltip(n: FGNode): string {
  const entries = Object.entries(n.properties).filter(
    ([, v]) => v !== null && v !== undefined && v !== '',
  )
  const rows = entries
    .slice(0, 8)
    .map(([k, v]) => {
      const display =
        typeof v === 'string' && v.length > 60 ? v.slice(0, 60) + '…' : String(v)
      return `<div style="display:flex;gap:8px"><span style="color:#6b7280;min-width:64px">${k}:</span><span style="color:#d1d5db">${display}</span></div>`
    })
    .join('')
  const more = entries.length > 8 ? `<div style="color:#6b7280;margin-top:4px">+${entries.length - 8} more…</div>` : ''
  return [
    `<div style="font-weight:600;color:#f1f5f9;margin-bottom:4px">${n.label}</div>`,
    `<div style="color:#475569;font-size:10px;margin-bottom:6px">${n.id}</div>`,
    rows,
    more,
  ].join('')
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
      properties: n.properties,
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
      nodeLabel={(n) => buildTooltip(n as FGNode)}
      nodeColor={(n) => NODE_HEX[(n as FGNode).type] || NODE_HEX.Unknown}
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
