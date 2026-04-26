import Drawer from '@/components/Drawer'
import JsonViewer from '@/components/JsonViewer'
import type { RunSummary } from '@/lib/types'
import { cn } from '@/lib/cn'

// ─── helpers ────────────────────────────────────────────────────────────────

function fmt(val: string | null | undefined): string {
  if (!val) return '—'
  return new Date(val).toLocaleString()
}

function pct(val: number | null | undefined): string {
  if (val == null) return '—'
  return `${(val * 100).toFixed(1)}%`
}

function GoalHealthColor(score: number | null): string {
  if (score == null) return 'var(--color-muted-foreground)'
  if (score >= 0.8) return 'var(--color-success)'
  if (score >= 0.5) return 'var(--color-warning)'
  return 'var(--color-destructive)'
}

function KV({ label, value, danger }: { label: string; value: React.ReactNode; danger?: boolean }) {
  return (
    <>
      <dt className="text-xs text-[var(--color-muted-foreground)] whitespace-nowrap pt-0.5">
        {label}
      </dt>
      <dd
        className={cn(
          'text-xs break-words font-mono',
          danger ? 'text-[var(--color-destructive)] font-semibold' : 'text-[var(--color-foreground)]',
        )}
      >
        {value ?? '—'}
      </dd>
    </>
  )
}

function DlGrid({ children }: { children: React.ReactNode }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">{children}</dl>
  )
}

// Collapsible section using native <details>
function Section({
  title,
  defaultOpen = false,
  children,
}: {
  title: string
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  return (
    <details
      open={defaultOpen}
      className="group rounded-md border border-[var(--color-border)] bg-[var(--color-card-elevated)] overflow-hidden"
    >
      <summary
        className={cn(
          'flex items-center justify-between px-4 py-2.5 cursor-pointer select-none list-none',
          'text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--color-muted-foreground)]',
          'hover:bg-[var(--color-muted)]/40 transition-colors',
          '[&::-webkit-details-marker]:hidden',
        )}
      >
        <span>{title}</span>
        {/* Chevron that rotates */}
        <svg
          className="h-3.5 w-3.5 transition-transform group-open:rotate-180"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
          aria-hidden
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </summary>
      <div className="px-4 pb-4 pt-2">{children}</div>
    </details>
  )
}

// ─── main component ──────────────────────────────────────────────────────────

export default function RunDetailDrawer({
  run,
  onClose,
}: {
  run: RunSummary | null
  onClose: () => void
}) {
  return (
    <Drawer
      open={run !== null}
      onClose={onClose}
      title={run ? `Run — ${fmt(run.run_at)}` : ''}
      width="620px"
    >
      {run && <RunContent run={run} />}
    </Drawer>
  )
}

function RunContent({ run }: { run: RunSummary }) {
  const goalColor = GoalHealthColor(run.goal_health)
  const errorCount = run.errors?.length ?? 0

  // Collect per-agent run count fields (keys ending with _agent_runs)
  const agentFields = Object.entries(run).filter(
    ([k]) => k.endsWith('_agent_runs') && typeof run[k] === 'number',
  ) as [string, number][]

  return (
    <div className="space-y-3">
      {/* ── 1. Headline (always open) ───────────────────────────── */}
      <Section title="Headline" defaultOpen>
        <DlGrid>
          <KV label="Run at" value={fmt(run.run_at)} />
          <KV label="Mode" value={run.mode} />
          <KV
            label="Goal health"
            value={
              run.goal_health != null ? (
                <span style={{ color: goalColor }} className="font-bold">
                  {pct(run.goal_health)}
                </span>
              ) : (
                '—'
              )
            }
          />
          <KV
            label="Errors"
            value={errorCount}
            danger={errorCount > 0}
          />
          <KV
            label="Upgrade available"
            value={
              run.upgrade_available != null
                ? String(run.upgrade_available)
                : '—'
            }
          />
        </DlGrid>
      </Section>

      {/* ── 2. PR Metrics ───────────────────────────────────────── */}
      <Section title="PR Metrics" defaultOpen>
        <DlGrid>
          <KV label="Monitored" value={run.prs_monitored} />
          <KV label="Merged" value={run.prs_merged} />
          <KV label="Escalated" value={run.prs_escalated} />
          <KV label="Fix requested" value={run.prs_fix_requested} />
          <KV label="Orphaned" value={run.orphaned_prs} />
          <KV
            label="Avg time to merge (h)"
            value={run.avg_time_to_merge_hours?.toFixed(1) ?? '—'}
          />
          <KV label="Escalation rate" value={pct(run.escalation_rate)} />
          <KV label="Copilot success rate" value={pct(run.copilot_success_rate)} />
        </DlGrid>
      </Section>

      {/* ── 3. Issue Metrics ────────────────────────────────────── */}
      <Section title="Issue Metrics" defaultOpen>
        <DlGrid>
          <KV label="Triaged" value={run.issues_triaged} />
          <KV label="Assigned" value={run.issues_assigned} />
          <KV label="Closed" value={run.issues_closed} />
          <KV label="Escalated" value={run.issues_escalated} />
          <KV label="Stale assignments escalated" value={run.stale_assignments_escalated} />
        </DlGrid>
      </Section>

      {/* ── 4. Ownership & Readiness ────────────────────────────── */}
      <Section title="Ownership &amp; Readiness" defaultOpen>
        <DlGrid>
          <KV label="Owned PRs" value={(run.owned_prs as number | undefined) ?? '—'} />
          <KV
            label="Readiness pass rate"
            value={
              run.readiness_pass_rate != null
                ? pct(run.readiness_pass_rate as number)
                : '—'
            }
          />
          <KV
            label="Avg readiness score"
            value={
              run.avg_readiness_score != null
                ? (run.avg_readiness_score as number).toFixed(2)
                : '—'
            }
          />
          <KV
            label="Authority merges"
            value={(run.authority_merges as number | undefined) ?? '—'}
          />
        </DlGrid>
      </Section>

      {/* ── 5. Per-Agent ────────────────────────────────────────── */}
      {agentFields.length > 0 && (
        <Section title="Per-Agent Run Counts">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
            {agentFields.map(([key, count]) => (
              <div key={key} className="flex items-center justify-between">
                <span className="text-xs text-[var(--color-muted-foreground)] truncate pr-2">
                  {key.replace(/_agent_runs$/, '').replace(/_/g, ' ')}
                </span>
                <span className="text-xs font-mono font-semibold text-[var(--color-foreground)] tabular-nums">
                  {count}
                </span>
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* ── 6. Errors ───────────────────────────────────────────── */}
      <Section title={`Errors${errorCount > 0 ? ` (${errorCount})` : ''}`} defaultOpen={errorCount > 0}>
        {errorCount > 0 ? (
          <ul className="space-y-1.5">
            {run.errors.map((err, i) => (
              <li
                key={i}
                className="text-xs text-[var(--color-destructive)] font-mono bg-[var(--color-muted)]/40 rounded px-2 py-1 break-words"
              >
                {err}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-[var(--color-muted-foreground)]">None</p>
        )}
      </Section>

      {/* ── 7. Raw JSON ─────────────────────────────────────────── */}
      <Section title="Raw">
        <JsonViewer data={run} maxHeight="350px" />
      </Section>
    </div>
  )
}
