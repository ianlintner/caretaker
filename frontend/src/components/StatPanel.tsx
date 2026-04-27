import type { ReactNode } from 'react'
import { ResponsiveContainer, AreaChart, Area } from 'recharts'
import { cn } from '@/lib/cn'

type Trend = 'up' | 'down' | 'flat' | undefined

export type StatPanelProps = {
  label: string
  value: ReactNode
  hint?: ReactNode
  trend?: Trend
  deltaLabel?: string
  sparkline?: { value: number }[]
  accentVar?: string
  footer?: ReactNode
}

const TREND_COLOR: Record<Exclude<Trend, undefined>, string> = {
  up: 'var(--color-success)',
  down: 'var(--color-destructive)',
  flat: 'var(--color-muted-foreground)',
}

export default function StatPanel({
  label,
  value,
  hint,
  trend,
  deltaLabel,
  sparkline,
  accentVar = '--chart-1',
  footer,
}: StatPanelProps) {
  const accent = `var(${accentVar})`
  const gradientId = `sp-grad-${accentVar.replace(/[^a-z0-9]/gi, '')}`

  return (
    <div className={cn('panel p-4 flex flex-col gap-3 overflow-hidden')}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-muted-foreground)] truncate">
            {label}
          </p>
          <p className="stat-value mt-1 text-3xl font-semibold leading-tight tracking-tight">
            {value}
          </p>
          {hint && (
            <p className="text-xs text-[var(--color-muted-foreground)] mt-1">
              {hint}
            </p>
          )}
        </div>
        {deltaLabel && (
          <span
            className="mono text-[11px] font-medium"
            style={{ color: trend ? TREND_COLOR[trend] : 'var(--color-muted-foreground)' }}
          >
            {deltaLabel}
          </span>
        )}
      </div>
      {sparkline && sparkline.length > 1 && (
        <div className="h-10 -mx-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={sparkline}
              margin={{ top: 4, right: 4, bottom: 0, left: 4 }}
            >
              <defs>
                <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor={accent} stopOpacity={0.45} />
                  <stop offset="100%" stopColor={accent} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area
                type="monotone"
                dataKey="value"
                stroke={accent}
                strokeWidth={1.5}
                fill={`url(#${gradientId})`}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      {footer && (
        <div className="text-xs text-[var(--color-muted-foreground)]">{footer}</div>
      )}
    </div>
  )
}
