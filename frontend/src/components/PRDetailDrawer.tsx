import { AlertTriangle, Bot, CheckCircle, Hand, ExternalLink } from 'lucide-react'
import Drawer from '@/components/Drawer'
import StatusBadge from '@/components/StatusBadge'
import { cn } from '@/lib/cn'
import type { TrackedPR } from '@/lib/types'

// ─── helpers ────────────────────────────────────────────────────────────────

function fmt(val: string | null | undefined): string {
  if (!val) return '—'
  return new Date(val).toLocaleString()
}

function ReadinessColor(score: number): string {
  if (score >= 0.8) return 'var(--color-success)'
  if (score >= 0.5) return 'var(--color-warning)'
  return 'var(--color-destructive)'
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
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium',
        'border',
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

export default function PRDetailDrawer({
  pr,
  onClose,
}: {
  pr: TrackedPR | null
  onClose: () => void
}) {
  return (
    <Drawer open={pr !== null} onClose={onClose} title={pr ? `PR #${pr.number}` : ''} width="580px">
      {pr && <PRContent pr={pr} />}
    </Drawer>
  )
}

function PRContent({ pr }: { pr: TrackedPR }) {
  const readinessColor = ReadinessColor(pr.readiness_score)

  return (
    <div className="space-y-6">
      {/* ── 1. Header ──────────────────────────────────────────────── */}
      <div className="space-y-2">
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-3xl font-bold font-mono text-[var(--color-foreground)]">
            #{pr.number}
          </span>
          <StatusBadge value={pr.state} />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <BoolChip
            label="Bot touched"
            value={!!pr.caretaker_touched}
            icon={Bot}
            activeColor="var(--color-primary)"
          />
          <BoolChip
            label="Bot merged"
            value={!!pr.caretaker_merged}
            icon={CheckCircle}
            activeColor="var(--color-success)"
          />
          <BoolChip
            label="Operator intervened"
            value={!!pr.operator_intervened}
            icon={Hand}
            activeColor="var(--color-warning)"
          />
        </div>
      </div>

      {/* ── 2. Readiness ───────────────────────────────────────────── */}
      <div className="panel p-4 space-y-3">
        <SectionHeading>Readiness</SectionHeading>
        <div className="flex items-end gap-4">
          <span
            className="text-5xl font-bold tabular-nums leading-none"
            style={{ color: readinessColor }}
          >
            {(pr.readiness_score * 100).toFixed(0)}
            <span className="text-2xl">%</span>
          </span>
          <div className="flex-1 pb-1">
            <div className="h-2 rounded-full bg-[var(--color-muted)] overflow-hidden">
              <div
                className="h-full rounded-full transition-all"
                style={{
                  width: `${Math.round(pr.readiness_score * 100)}%`,
                  backgroundColor: readinessColor,
                }}
              />
            </div>
          </div>
        </div>
        {pr.readiness_summary && (
          <p className="text-sm text-[var(--color-foreground)]">{pr.readiness_summary}</p>
        )}
        {pr.readiness_blockers.length > 0 && (
          <ul className="space-y-1">
            {pr.readiness_blockers.map((b, i) => (
              <li key={i} className="flex items-start gap-2 text-xs text-[var(--color-warning)]">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
                <span className="text-[var(--color-foreground)]">{b}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* ── 3. Attribution ─────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Attribution</SectionHeading>
        <DlGrid
          rows={[
            [
              'Bot touched',
              <BoolChip key="ct" label={pr.caretaker_touched ? 'Yes' : 'No'} value={!!pr.caretaker_touched} icon={Bot} />,
            ],
            [
              'Bot merged',
              <BoolChip key="cm" label={pr.caretaker_merged ? 'Yes' : 'No'} value={!!pr.caretaker_merged} icon={CheckCircle} activeColor="var(--color-success)" />,
            ],
            [
              'Operator intervened',
              <BoolChip key="oi" label={pr.operator_intervened ? 'Yes' : 'No'} value={!!pr.operator_intervened} icon={Hand} activeColor="var(--color-warning)" />,
            ],
            [
              'Intervention reasons',
              pr.intervention_reasons?.length
                ? pr.intervention_reasons.join(', ')
                : '—',
            ],
            ['Last bot action', fmt(pr.last_caretaker_action_at)],
          ]}
        />
      </div>

      {/* ── 4. Activity ────────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Activity</SectionHeading>
        <DlGrid
          rows={[
            ['Fix cycles', <span key="fc" className="font-mono font-semibold">{pr.fix_cycles}</span>],
            ['Copilot attempts', pr.copilot_attempts],
            ['CI attempts', pr.ci_attempts],
            [
              'Escalated',
              <BoolChip key="esc" label={pr.escalated ? 'Yes' : 'No'} value={pr.escalated} activeColor="var(--color-destructive)" />,
            ],
          ]}
        />
      </div>

      {/* ── 5. Ownership ───────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Ownership</SectionHeading>
        <DlGrid
          rows={[
            [
              'Ownership state',
              <OwnershipBadge key="os" value={pr.ownership_state} />,
            ],
            ['Owned by', pr.owned_by || '—'],
            ['Acquired at', fmt(pr.ownership_acquired_at)],
            ['Released at', fmt(pr.released_at)],
          ]}
        />
      </div>

      {/* ── 6. Timeline ────────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Timeline</SectionHeading>
        <DlGrid
          rows={[
            ['Last checked', fmt(pr.last_checked)],
            ['First seen', fmt(pr.first_seen_at)],
            ['Merged at', fmt(pr.merged_at)],
            ['Last state change', fmt(pr.last_state_change_at)],
          ]}
        />
      </div>

      {/* ── 7. Notes ───────────────────────────────────────────────── */}
      {pr.notes && (
        <div className="panel p-4">
          <SectionHeading>Notes</SectionHeading>
          <p className="text-sm text-[var(--color-foreground)] whitespace-pre-wrap">{pr.notes}</p>
        </div>
      )}

      {/* ── 8. GitHub link ─────────────────────────────────────────── */}
      <div className="panel p-4">
        <SectionHeading>Links</SectionHeading>
        <a
          href={`#pr-${pr.number}`}
          className="inline-flex items-center gap-1.5 text-xs text-[var(--color-primary)] hover:underline"
          aria-label={`View PR #${pr.number} on GitHub`}
        >
          <ExternalLink className="h-3.5 w-3.5" />
          View PR #{pr.number} on GitHub
        </a>
        <p className="text-[11px] text-[var(--color-muted-foreground)] mt-1">
          Repo URL not available in this context — navigate to the GitHub repository and open pull/{pr.number}.
        </p>
      </div>
    </div>
  )
}

function OwnershipBadge({ value }: { value: string }) {
  const colorMap: Record<string, string> = {
    owned: 'var(--color-success)',
    unowned: 'var(--color-muted-foreground)',
    released: 'var(--color-primary)',
    escalated: 'var(--color-destructive)',
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
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: color }}
        aria-hidden
      />
      {value || '—'}
    </span>
  )
}
