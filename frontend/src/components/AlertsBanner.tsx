import { useState } from 'react'
import { X, AlertTriangle, AlertCircle } from 'lucide-react'
import { Link } from 'react-router-dom'
import useSWR from 'swr'
import type { FleetAlert } from '@/lib/types'

export default function AlertsBanner() {
  const [dismissed, setDismissed] = useState(() =>
    sessionStorage.getItem('alerts-banner-dismissed') === 'true',
  )

  const { data } = useSWR<{ items: FleetAlert[] }>(
    '/api/admin/fleet/alerts?open=true',
    { refreshInterval: 60_000 },
  )

  const alerts = data?.items?.filter((a) => a.resolved_at === null) ?? []
  const hasCritical = alerts.some((a) => a.severity === 'critical')

  if (alerts.length === 0 || dismissed) return null

  function dismiss() {
    sessionStorage.setItem('alerts-banner-dismissed', 'true')
    setDismissed(true)
  }

  const bg = hasCritical ? 'rgba(239,68,68,0.1)' : 'rgba(245,158,11,0.1)'
  const border = hasCritical ? 'var(--color-destructive)' : 'var(--color-warning)'
  const color = hasCritical ? 'var(--color-destructive)' : 'var(--color-warning)'
  const Icon = hasCritical ? AlertCircle : AlertTriangle

  return (
    <div
      className="flex items-center gap-3 px-5 py-2 text-sm shrink-0"
      style={{
        background: bg,
        borderBottom: `1px solid ${border}`,
        color,
      }}
    >
      <Icon className="h-4 w-4 shrink-0" />
      <span className="font-medium">
        {alerts.length} open fleet alert{alerts.length > 1 ? 's' : ''}
        {hasCritical ? ' (critical)' : ''}
      </span>
      <span className="text-[var(--color-muted-foreground)] text-xs">
        {alerts.slice(0, 2).map((a) => a.repo).join(', ')}
        {alerts.length > 2 ? ` +${alerts.length - 2} more` : ''}
      </span>
      <Link
        to="/alerts"
        className="ml-auto text-xs underline hover:opacity-80"
        style={{ color }}
      >
        View all →
      </Link>
      <button
        onClick={dismiss}
        className="p-0.5 rounded hover:opacity-70"
        aria-label="Dismiss"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
