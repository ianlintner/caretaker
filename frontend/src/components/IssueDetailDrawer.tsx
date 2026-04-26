import { Bot, CheckCircle, Hand } from 'lucide-react'
import { Link } from 'react-router-dom'
import Drawer from '@/components/Drawer'
import StatusBadge from '@/components/StatusBadge'
import { cn } from '@/lib/cn'
import type { TrackedIssue } from '@/lib/types'

// ─── helpers ────────────────────────────────────────────────────────────────

function fmt(val: string | null | undefined): string {
  if (!val) return '—'
  return new Date(val).toLocaleString()
}

function BoolChip({
  label,
  value,
  icon: Icon,
  activeColor = 'var(--color-primary)',
}: {
  label: string
  value: boolean
  icon?: React.ComponentType<{ className?: string }>
  activeColor?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border',
      )}
      style={
        value
          ? {
              backgroundColor: `color-mix(in srgb, ${activeColor} 15%, transparent)`,
              color: activeColor,
              borderColor: `color-mix(in srgb, ${activeColor} 35%, transparent)`,
            }
          : {
              backgroundColor: 'transparent',
              color: 'var(--color-muted-foreground)',
              borderColor: 'var(--color-border)',
            }
      }
    >
      {Icon && <Icon className="h-3 w-3" />}
      {label}
    </span>
  )
}

function ClassificationBadge({ value }: { value: string }) {
  const colorMap: Record<string, string> = {
    bug: 'var(--color-destructive)',
    security: 'var(--color-destructive)',
    feature: 'var(--color-primary)',
    question: 'var(--color-warning)',
    maintenance: 'var(--color-muted-foreground)',
    other: 'var(--color-muted-foreground)',
  }
  const color = colorMap[value?.toLowerCase()] ?? 'var(--color-muted-foreground)'
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border"
      style={{
        color,
        borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
        backgroundColor: `color-mix(in srgb, ${color} 12%, transparent)`,
      }}
    >
      {value || 'unclassified'}
    </span>
  )
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--color-muted-foreground)] mb-2">
      {children}
    </h3>
  )
}

function DlGrid({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
      {rows.map(([label, value]) => (
        <>
          <dt
            key={`dt-${label}`}
            className="text-xs text-[var(--color-muted-foreground)] whitespace-nowrap pt-0.5"
          >
            {label}
          </dt>
          <dd key={`dd-${label}`} className="text-xs text-[var(--color-foreground)] break-words">
            {value ?? '—'}
          </dd>
        </>
      ))}
    </dl>
  )
}

// ─── main component ──────────────────────────────────────────────────────────

export default function IssueDetailDrawer({
  issue,
  onClose,
}: {
  issue: TrackedIssue | null
  onClose: () => void
}) {
  return (
    <Drawer
      open={issue !== null}
      onClose={onClose}
      title={issue ? `Issue #${issue.number}` : ''}
      width="520px"
    >
      {issue && <IssueContent issue={issue} />}
    </Drawer>
  )
}

function IssueContent({ issue }: { issue: TrackedIssue }) {
  return (
    <div className="space-y-6">
      {/* ── 1. Header ──────────────────────────────────────────────── */}
      <div className="space-y-2">
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-3xl font-bold font-mono text-[var(--color-foreground)]">
            #{issue.number}
          </span>
          <StatusBadge value={issue.state} />
          {issue.classification && (
            <ClassificationBadge value={issue.classification} />
          )}
        </div>
      </div>

      {/* ── 2. Attribution ─────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Attribution</SectionHeading>
        <div className="flex flex-wrap gap-2 mb-3">
          <BoolChip
            label="Bot touched"
            value={!!issue.caretaker_touched}
            icon={Bot}
            activeColor="var(--color-primary)"
          />
          <BoolChip
            label="Bot closed"
            value={!!issue.caretaker_closed}
            icon={CheckCircle}
            activeColor="var(--color-success)"
          />
          <BoolChip
            label="Operator intervened"
            value={!!issue.operator_intervened}
            icon={Hand}
            activeColor="var(--color-warning)"
          />
        </div>
        <DlGrid
          rows={[
            [
              'Intervention reasons',
              issue.intervention_reasons?.length
                ? issue.intervention_reasons.join(', ')
                : '—',
            ],
            ['Last bot action', fmt(issue.last_caretaker_action_at)],
          ]}
        />
      </div>

      {/* ── 3. Assignment ──────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Assignment</SectionHeading>
        <DlGrid
          rows={[
            [
              'Assigned PR',
              issue.assigned_pr != null ? (
                <Link
                  key="pr-link"
                  to="/prs"
                  className="inline-flex items-center gap-1 text-[var(--color-primary)] hover:underline font-mono"
                  title={`Go to PR list and find #${issue.assigned_pr}`}
                >
                  #{issue.assigned_pr}
                </Link>
              ) : (
                '—'
              ),
            ],
          ]}
        />
      </div>

      {/* ── 4. Status ──────────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Status</SectionHeading>
        <DlGrid
          rows={[
            [
              'Escalated',
              <BoolChip
                key="esc"
                label={issue.escalated ? 'Yes' : 'No'}
                value={issue.escalated}
                activeColor="var(--color-destructive)"
              />,
            ],
            ['Last checked', fmt(issue.last_checked)],
          ]}
        />
      </div>
    </div>
  )
}
