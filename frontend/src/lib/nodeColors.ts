export type NodeColor = { bg: string; border: string; text: string }

export const NODE_COLOR_MAP: Record<string, NodeColor> = {
  Agent: { bg: '#ddd6fe', border: '#7c3aed', text: '#4c1d95' },
  PR: { bg: '#d1fae5', border: '#059669', text: '#064e3b' },
  Issue: { bg: '#fee2e2', border: '#dc2626', text: '#7f1d1d' },
  Goal: { bg: '#fef3c7', border: '#d97706', text: '#78350f' },
  Skill: { bg: '#dbeafe', border: '#2563eb', text: '#1e3a8a' },
  Run: { bg: '#e5e7eb', border: '#6b7280', text: '#1f2937' },
  AuditEvent: { bg: '#f3e8ff', border: '#a855f7', text: '#581c87' },
  Mutation: { bg: '#fce7f3', border: '#db2777', text: '#831843' },
  Repo: { bg: '#ecfdf5', border: '#10b981', text: '#064e3b' },
  Comment: { bg: '#f0fdf4', border: '#22c55e', text: '#14532d' },
  CheckRun: { bg: '#fff7ed', border: '#f97316', text: '#7c2d12' },
  Executor: { bg: '#fdf4ff', border: '#c026d3', text: '#701a75' },
  RunSummaryWeek: { bg: '#f8fafc', border: '#94a3b8', text: '#334155' },
  GlobalSkill: { bg: '#eff6ff', border: '#3b82f6', text: '#1e3a8a' },
  AgentCoreMemory: { bg: '#fef9c3', border: '#eab308', text: '#713f12' },
  CausalEvent: { bg: '#fef3c7', border: '#f59e0b', text: '#78350f' },
  Unknown: { bg: '#f4f4f5', border: '#a1a1aa', text: '#27272a' },
}

export function nodeColor(type: string): NodeColor {
  return NODE_COLOR_MAP[type] ?? NODE_COLOR_MAP.Unknown
}

export const NODE_HEX: Record<string, string> = Object.fromEntries(
  Object.entries(NODE_COLOR_MAP).map(([k, v]) => [k, v.border]),
)
