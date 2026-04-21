import { useState } from 'react'
import useSWR from 'swr'
import Graph2DView from '@/components/Graph2DView'
import type { GoalSnapshot, SubGraph } from '@/lib/types'

export default function GoalImpactView() {
  const { data: goals } = useSWR<Record<string, GoalSnapshot[]>>('/api/admin/goals')
  const goalIds = goals ? Object.keys(goals) : []

  const [selectedGoal, setSelectedGoal] = useState<string>('')
  const [depth, setDepth] = useState(2)

  const effectiveGoal = selectedGoal || goalIds[0] || ''
  const nodeId = effectiveGoal ? `goal:${effectiveGoal}` : null

  const { data: subgraph, isLoading } = useSWR<SubGraph>(
    nodeId ? `/api/graph/neighbors/${encodeURIComponent(nodeId)}?depth=${depth}` : null,
  )

  const goalHistory = effectiveGoal && goals ? goals[effectiveGoal] ?? [] : []
  const latestScore = goalHistory.at(-1)?.score

  return (
    <div className="flex flex-col h-full">
      {/* Controls bar */}
      <div className="p-4 border-b border-[var(--color-border)] flex items-center gap-4">
        <div className="flex items-center gap-2">
          <label className="text-xs text-[var(--color-muted-foreground)]">Goal</label>
          {goalIds.length === 0 ? (
            <span className="text-xs text-[var(--color-muted-foreground)]">No goals recorded</span>
          ) : (
            <select
              value={effectiveGoal}
              onChange={(e) => setSelectedGoal(e.target.value)}
              className="border border-[var(--color-border)] rounded px-2 py-1 text-xs bg-transparent"
            >
              {goalIds.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs text-[var(--color-muted-foreground)]">Depth</label>
          <select
            value={depth}
            onChange={(e) => setDepth(Number(e.target.value))}
            className="border border-[var(--color-border)] rounded px-2 py-1 text-xs bg-transparent"
          >
            <option value={1}>1</option>
            <option value={2}>2</option>
            <option value={3}>3</option>
          </select>
        </div>
        {effectiveGoal && latestScore != null && (
          <div className="ml-auto text-xs">
            <span className="text-[var(--color-muted-foreground)]">Latest score: </span>
            <span
              className="font-mono font-semibold"
              style={{
                color:
                  latestScore >= 0.7
                    ? 'var(--color-success)'
                    : latestScore >= 0.4
                      ? 'var(--color-warning)'
                      : 'var(--color-destructive)',
              }}
            >
              {latestScore.toFixed(3)}
            </span>
            <span className="text-[var(--color-muted-foreground)] ml-2">
              ({goalHistory.length} snapshots)
            </span>
          </div>
        )}
      </div>

      {/* Graph area */}
      <div className="flex-1 relative">
        {!effectiveGoal ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
            No goals configured
          </div>
        ) : isLoading ? (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-[var(--color-muted-foreground)]">
            Loading impact graph…
          </div>
        ) : !subgraph || subgraph.nodes.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center space-y-2">
              <p className="text-sm text-[var(--color-muted-foreground)]">
                No graph data for <span className="font-mono">{effectiveGoal}</span>
              </p>
              <p className="text-xs text-[var(--color-muted-foreground)]">
                Goal node will appear once a run completes with this goal id.
              </p>
            </div>
          </div>
        ) : (
          <Graph2DView subgraph={subgraph} />
        )}
      </div>
    </div>
  )
}
