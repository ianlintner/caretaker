import { useState } from 'react'
import useSWR from 'swr'
import { useNavigate, useParams } from 'react-router-dom'
import PageHeader from '@/components/PageHeader'
import { DataTable, type Column } from '@/components/DataTable'
import Pagination from '@/components/Pagination'
import { cn } from '@/lib/cn'
import type { MemoryEntry, MemoryNamespace, Paginated, Skill, SubGraph, GraphNode } from '@/lib/types'

type MemoryTab = 'namespaces' | 'agents' | 'skills'

// ── Agent core memory ──────────────────────────────────────────────────────

type AgentInfo = { name: string; modes: string[]; events: string[] }

function AgentCoreMemoryPanel({ agentName }: { agentName: string }) {
  // Fetch AgentCoreMemory nodes from graph for this agent
  const { data: subgraph } = useSWR<SubGraph>(
    `/api/graph/subgraph?types=AgentCoreMemory&limit=50`,
  )
  const { data: recentActions } = useSWR<{ actions: unknown[] }>(
    `/api/mcp/memory/recent-actions?agent=${encodeURIComponent(agentName)}&limit=20`,
  )

  const coreNodes: GraphNode[] =
    subgraph?.nodes.filter(
      (n) =>
        n.type === 'AgentCoreMemory' &&
        (n.properties?.agent === agentName || n.label.includes(agentName)),
    ) ?? []

  const latest = coreNodes[0]

  return (
    <div className="space-y-4">
      <div className="panel p-4">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)] mb-3">
          Core memory — {agentName}
        </h4>
        {!latest ? (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            No AgentCoreMemory node found. Agent must run at least once with Neo4j configured.
          </p>
        ) : (
          <dl className="grid grid-cols-2 gap-y-2 text-xs">
            {Object.entries(latest.properties).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-[var(--color-muted-foreground)] font-medium">{k}</dt>
                <dd className="font-mono break-all">
                  {v == null ? '—' : Array.isArray(v) ? v.join(', ') || '—' : String(v)}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {recentActions && recentActions.actions?.length > 0 && (
        <div className="panel p-4">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)] mb-3">
            Recent actions
          </h4>
          <div className="space-y-1">
            {recentActions.actions.map((a, i) => (
              <div
                key={i}
                className="text-xs font-mono text-[var(--color-muted-foreground)] truncate"
              >
                {typeof a === 'string' ? a : JSON.stringify(a)}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function AgentCoreMemoryBrowser() {
  const { data: agents } = useSWR<AgentInfo[]>('/api/admin/agents')
  const [selectedAgent, setSelectedAgent] = useState<string>('')

  const agentList = agents ?? []
  const effective = selectedAgent || agentList[0]?.name || ''

  return (
    <div className="space-y-4">
      {agentList.length === 0 ? (
        <p className="text-sm text-[var(--color-muted-foreground)]">No agents registered.</p>
      ) : (
        <>
          <div className="flex items-center gap-3">
            <label className="text-xs text-[var(--color-muted-foreground)]">Agent</label>
            <select
              value={effective}
              onChange={(e) => setSelectedAgent(e.target.value)}
              className="border border-[var(--color-border)] rounded px-2 py-1 text-xs bg-transparent"
            >
              {agentList.map((a) => (
                <option key={a.name} value={a.name}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>
          {effective && <AgentCoreMemoryPanel agentName={effective} />}
        </>
      )}
    </div>
  )
}

// ── Skill drilldown ────────────────────────────────────────────────────────

function SkillDetail({ skill }: { skill: Skill }) {
  const nodeId = `skill:${skill.id}`
  const { data: subgraph } = useSWR<SubGraph>(
    `/api/graph/neighbors/${encodeURIComponent(nodeId)}?depth=2`,
  )

  const totalRuns = skill.success_count + skill.fail_count
  const successRate = totalRuns > 0 ? (skill.success_count / totalRuns) * 100 : null

  return (
    <div className="space-y-4">
      <div className="panel p-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-semibold text-[var(--color-muted-foreground)] uppercase tracking-wide">
              {skill.category}
            </div>
            <div className="font-mono text-xs mt-1 text-[var(--color-muted-foreground)]">
              {skill.signature}
            </div>
          </div>
          <div className="text-right shrink-0">
            <div
              className="text-lg font-semibold font-mono"
              style={{
                color:
                  skill.confidence >= 0.7
                    ? 'var(--color-success)'
                    : skill.confidence >= 0.4
                      ? 'var(--color-warning)'
                      : 'var(--color-destructive)',
              }}
            >
              {(skill.confidence * 100).toFixed(0)}%
            </div>
            <div className="text-xs text-[var(--color-muted-foreground)]">confidence</div>
          </div>
        </div>

        {skill.sop_text && (
          <div className="text-xs text-[var(--color-foreground)] bg-[var(--color-muted)] rounded p-3 whitespace-pre-wrap font-mono leading-relaxed">
            {skill.sop_text}
          </div>
        )}

        <dl className="grid grid-cols-3 gap-3 text-xs">
          <div>
            <dt className="text-[var(--color-muted-foreground)]">Successes</dt>
            <dd className="font-mono font-semibold" style={{ color: 'var(--color-success)' }}>
              {skill.success_count}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-muted-foreground)]">Failures</dt>
            <dd
              className="font-mono font-semibold"
              style={{
                color: skill.fail_count > 0 ? 'var(--color-destructive)' : undefined,
              }}
            >
              {skill.fail_count}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-muted-foreground)]">Success rate</dt>
            <dd className="font-mono font-semibold">
              {successRate != null ? `${successRate.toFixed(0)}%` : '—'}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-muted-foreground)]">Last used</dt>
            <dd className="font-mono">
              {skill.last_used_at
                ? new Date(skill.last_used_at).toLocaleDateString()
                : '—'}
            </dd>
          </div>
          <div>
            <dt className="text-[var(--color-muted-foreground)]">Created</dt>
            <dd className="font-mono">{new Date(skill.created_at).toLocaleDateString()}</dd>
          </div>
        </dl>
      </div>

      {subgraph && subgraph.nodes.length > 0 && (
        <div className="panel p-4">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)] mb-2">
            Graph neighborhood ({subgraph.nodes.length} nodes, {subgraph.edges.length} edges)
          </h4>
          <p className="text-xs text-[var(--color-muted-foreground)] mb-3">
            Connected CausalEvents, Runs, and Goals that validated this skill.
          </p>
          <div className="space-y-1">
            {subgraph.nodes
              .filter((n) => n.id !== nodeId)
              .map((n) => (
                <div
                  key={n.id}
                  className="flex items-center gap-2 text-xs px-2 py-1 rounded border border-[var(--color-border)]"
                >
                  <span className="text-[var(--color-muted-foreground)] w-24 shrink-0">
                    {n.type}
                  </span>
                  <span className="font-mono truncate">{n.label}</span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  )
}

function SkillBrowser() {
  const [category, setCategory] = useState('')
  const [offset, setOffset] = useState(0)
  const [selectedSkill, setSelectedSkill] = useState<Skill | null>(null)
  const limit = 20

  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (category) qs.set('category', category)
  const { data } = useSWR<Paginated<Skill>>(`/api/admin/skills?${qs}`)

  const columns: Column<Skill>[] = [
    {
      key: 'category',
      header: 'Category',
      render: (r) => <span className="text-xs">{r.category}</span>,
    },
    {
      key: 'signature',
      header: 'Signature',
      render: (r) => (
        <span className="font-mono text-xs text-[var(--color-muted-foreground)] truncate block max-w-xs">
          {r.signature}
        </span>
      ),
    },
    {
      key: 'confidence',
      header: 'Confidence',
      width: '100px',
      render: (r) => (
        <span
          className="font-mono text-xs"
          style={{
            color:
              r.confidence >= 0.7
                ? 'var(--color-success)'
                : r.confidence >= 0.4
                  ? 'var(--color-warning)'
                  : 'var(--color-destructive)',
          }}
        >
          {(r.confidence * 100).toFixed(0)}%
        </span>
      ),
    },
    {
      key: 'success_count',
      header: 'Runs',
      width: '80px',
      render: (r) => (
        <span className="font-mono text-xs text-[var(--color-muted-foreground)]">
          {r.success_count + r.fail_count}
        </span>
      ),
    },
  ]

  if (selectedSkill) {
    return (
      <div>
        <button
          onClick={() => setSelectedSkill(null)}
          className="mb-4 text-xs text-[var(--color-primary)] hover:underline"
        >
          ← All skills
        </button>
        <SkillDetail skill={selectedSkill} />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <input
          type="text"
          placeholder="Filter by category…"
          value={category}
          onChange={(e) => {
            setCategory(e.target.value)
            setOffset(0)
          }}
          className="border border-[var(--color-border)] rounded px-2 py-1 text-xs bg-transparent placeholder:text-[var(--color-muted-foreground)] w-48"
        />
      </div>
      <DataTable
        columns={columns}
        rows={data?.items ?? []}
        empty="No skills recorded."
        onRowClick={(r) => setSelectedSkill(r)}
      />
      {data && (
        <Pagination
          offset={data.offset}
          limit={data.limit}
          total={data.total}
          onChange={setOffset}
        />
      )}
    </div>
  )
}

// ── Namespace/entry views (unchanged) ──────────────────────────────────────

function NamespaceList() {
  const navigate = useNavigate()
  const { data, isLoading } = useSWR<MemoryNamespace[]>('/api/admin/memory')

  const columns: Column<MemoryNamespace>[] = [
    {
      key: 'namespace',
      header: 'Namespace',
      render: (r) => <span className="font-mono text-xs">{r.namespace}</span>,
    },
    {
      key: 'count',
      header: 'Keys',
      render: (r) => <span className="tabular-nums">{r.key_count}</span>,
    },
  ]

  if (isLoading) {
    return <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
  }

  return (
    <DataTable
      columns={columns}
      rows={data ?? []}
      empty="No memory namespaces."
      onRowClick={(r) => navigate(`/memory/${encodeURIComponent(r.namespace)}`)}
    />
  )
}

function EntryList({ namespace }: { namespace: string }) {
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const limit = 50
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  const { data, isLoading } = useSWR<Paginated<MemoryEntry>>(
    `/api/admin/memory/${encodeURIComponent(namespace)}?${qs}`,
  )

  const columns: Column<MemoryEntry>[] = [
    {
      key: 'key',
      header: 'Key',
      render: (r) => <span className="font-mono text-xs break-all">{r.key}</span>,
    },
    {
      key: 'value',
      header: 'Value',
      render: (r) => (
        <span className="text-xs text-[var(--color-muted-foreground)] line-clamp-2 block">
          {r.value}
        </span>
      ),
    },
    {
      key: 'updated',
      header: 'Updated',
      width: '180px',
      render: (r) => (
        <span className="text-xs text-[var(--color-muted-foreground)]">
          {r.updated_at ? new Date(r.updated_at).toLocaleString() : '—'}
        </span>
      ),
    },
  ]

  return (
    <>
      <button
        onClick={() => navigate('/memory')}
        className="mb-4 text-xs text-[var(--color-primary)] hover:underline"
      >
        ← All namespaces
      </button>
      {isLoading ? (
        <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
      ) : (
        <>
          <DataTable columns={columns} rows={data?.items ?? []} empty="No entries." />
          {data && (
            <Pagination
              offset={data.offset}
              limit={data.limit}
              total={data.total}
              onChange={setOffset}
            />
          )}
        </>
      )}
    </>
  )
}

// ── Root page ──────────────────────────────────────────────────────────────

export default function Memory() {
  const { namespace } = useParams<{ namespace?: string }>()
  const [tab, setTab] = useState<MemoryTab>('namespaces')

  if (namespace) {
    return (
      <>
        <PageHeader title={`Memory: ${namespace}`} description="Key/value entries in this namespace." />
        <div className="p-8">
          <EntryList namespace={namespace} />
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader title="Memory" description="Agent memory store, core memory, and learned skills." />

      {/* Tab bar */}
      <div className="border-b border-[var(--color-border)] px-6 flex items-center gap-0">
        {(
          [
            { id: 'namespaces', label: 'KV Namespaces' },
            { id: 'agents', label: 'Core Memory' },
            { id: 'skills', label: 'Skills' },
          ] as { id: MemoryTab; label: string }[]
        ).map(({ id, label }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={cn(
              'px-3 py-2 text-xs border-b-2 transition-colors',
              tab === id
                ? 'border-[var(--color-primary)] text-[var(--color-primary)] font-medium'
                : 'border-transparent text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="p-8">
        {tab === 'namespaces' && <NamespaceList />}
        {tab === 'agents' && <AgentCoreMemoryBrowser />}
        {tab === 'skills' && <SkillBrowser />}
      </div>
    </>
  )
}
