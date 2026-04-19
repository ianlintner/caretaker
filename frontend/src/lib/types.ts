export type UserInfo = {
  sub: string
  email: string | null
  name: string | null
  picture: string | null
}

export type Paginated<T> = {
  items: T[]
  total: number
  offset: number
  limit: number
}

export type TrackedPR = {
  number: number
  state: string
  first_seen_at: string | null
  merged_at: string | null
  ci_attempts: number
  copilot_attempts: number
  last_checked: string | null
  escalated: boolean
  notes: string
  ownership_state: string
  owned_by: string
  ownership_acquired_at: string | null
  readiness_score: number
  readiness_blockers: string[]
  readiness_summary: string
  fix_cycles: number
}

export type TrackedIssue = {
  number: number
  state: string
  classification: string
  assigned_pr: number | null
  last_checked: string | null
  escalated: boolean
}

export type RunSummary = {
  run_at: string
  mode: string
  prs_monitored: number
  prs_merged: number
  prs_escalated: number
  issues_triaged: number
  issues_assigned: number
  issues_closed: number
  issues_escalated: number
  orphaned_prs: number
  stale_assignments_escalated: number
  prs_fix_requested: number
  avg_time_to_merge_hours: number
  escalation_rate: number
  copilot_success_rate: number
  goal_health: number | null
  errors: string[]
  [key: string]: unknown
}

export type GoalSnapshot = {
  goal_id?: string
  score: number
  timestamp: string
  rationale?: string
  [key: string]: unknown
}

export type MemoryNamespace = {
  namespace: string
  key_count: number
}

export type MemoryEntry = {
  namespace: string
  key: string
  value: string
  created_at?: string
  updated_at?: string
  expires_at?: string
}

export type Skill = {
  id: string
  category: string
  signature: string
  sop_text: string
  success_count: number
  fail_count: number
  confidence: number
  last_used_at: string | null
  created_at: string
}

export type Mutation = {
  id?: string
  trial_name?: string
  parameter?: string
  value?: unknown
  status?: string
  started_at?: string
  ended_at?: string
  [key: string]: unknown
}

export type AgentInfo = {
  name: string
  modes: string[]
  events: string[]
}

export type GraphNode = {
  id: string
  type: string
  label: string
  properties: Record<string, unknown>
}

export type GraphEdge = {
  id: string
  source: string
  target: string
  type: string
  properties: Record<string, unknown>
}

export type SubGraph = {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export type GraphStats = {
  node_counts: Record<string, number>
  edge_counts: Record<string, number>
  total_nodes: number
  total_edges: number
}
