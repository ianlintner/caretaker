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
  caretaker_touched?: boolean
  caretaker_merged?: boolean
  operator_intervened?: boolean
  intervention_reasons?: string[]
  last_caretaker_action_at?: string | null
  released_at?: string | null
  last_state_change_at?: string | null
}

export type TrackedIssue = {
  number: number
  state: string
  classification: string
  assigned_pr: number | null
  last_checked: string | null
  escalated: boolean
  caretaker_touched?: boolean
  caretaker_closed?: boolean
  operator_intervened?: boolean
  intervention_reasons?: string[]
  last_caretaker_action_at?: string | null
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

export type CausalEvent = {
  id: string
  source: string
  parent_id: string | null
  run_id: string | null
  title: string
  observed_at: string | null
}

export type CausalChainResult = {
  id: string
  events: CausalEvent[]
  truncated?: boolean
}

export type CausalDescendantsResult = {
  id: string
  events: CausalEvent[]
}

export type FleetClient = {
  repo: string
  caretaker_version: string
  last_seen: string
  first_seen: string
  last_mode: string
  enabled_agents: string[]
  last_goal_health: number | null
  last_error_count: number
  last_counters: Record<string, number>
  last_summary: Record<string, unknown> | null
  heartbeats_seen: number
}

export type FleetClientDetail = FleetClient & {
  history?: Array<Record<string, unknown>>
}

export type FleetAlertKind =
  | 'goal_health_regression'
  | 'error_spike'
  | 'ghosted'
  | 'scope_gap'

export type FleetAlertSeverity = 'warning' | 'critical'

export type FleetAlert = {
  repo: string
  kind: FleetAlertKind
  severity: FleetAlertSeverity
  summary: string
  opened_at: string
  resolved_at: string | null
  details: Record<string, unknown>
}

export type FleetAlertList = {
  items: FleetAlert[]
  total: number
}
