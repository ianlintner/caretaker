import type { CSSProperties } from 'react'
import { cn } from '@/lib/cn'

type Tone = 'success' | 'danger' | 'warning' | 'info' | 'accent' | 'neutral'

const TONE_MAP: Record<string, Tone> = {
  open: 'success',
  passing: 'success',
  success: 'success',
  merged: 'accent',
  ready: 'accent',
  closed: 'danger',
  failed: 'danger',
  failing: 'danger',
  error: 'danger',
  escalated: 'danger',
  pending: 'warning',
  review_pending: 'warning',
  warning: 'warning',
  running: 'info',
  ci_pending: 'info',
  discovered: 'info',
  draft: 'neutral',
}

const TONE_STYLE: Record<Tone, CSSProperties> = {
  success: {
    backgroundColor: 'var(--color-success-soft)',
    color: 'var(--color-success)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-success) 30%, transparent)',
  },
  danger: {
    backgroundColor: 'var(--color-destructive-soft)',
    color: 'var(--color-destructive)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-destructive) 30%, transparent)',
  },
  warning: {
    backgroundColor: 'var(--color-warning-soft)',
    color: 'var(--color-warning)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-warning) 30%, transparent)',
  },
  info: {
    backgroundColor: 'var(--color-info-soft)',
    color: 'var(--color-info)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-info) 30%, transparent)',
  },
  accent: {
    backgroundColor: 'var(--color-primary-soft)',
    color: 'var(--color-primary)',
    boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--color-primary) 30%, transparent)',
  },
  neutral: {
    backgroundColor: 'var(--color-neutral-soft)',
    color: 'var(--color-muted-foreground)',
    boxShadow: 'inset 0 0 0 1px var(--color-border)',
  },
}

function toneFor(value: string): Tone {
  return TONE_MAP[value.toLowerCase()] ?? 'neutral'
}

export default function StatusBadge({ value }: { value: string }) {
  const tone = toneFor(value)
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full',
        'text-[11px] font-medium uppercase tracking-[0.04em]',
      )}
      style={TONE_STYLE[tone]}
    >
      <span
        aria-hidden
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: 'currentColor', opacity: 0.85 }}
      />
      {value}
    </span>
  )
}
