const modeStyles: Record<string, { bg: string; text: string; label: string }> = {
  off: { bg: 'var(--color-muted)', text: 'var(--color-muted-foreground)', label: 'Off' },
  shadow: { bg: 'rgba(245,158,11,0.15)', text: 'var(--color-warning)', label: 'Shadow' },
  enforce: { bg: 'rgba(34,197,94,0.15)', text: 'var(--color-success)', label: 'Enforce' },
}

export default function ModeBadge({ mode }: { mode: string }) {
  const s = modeStyles[mode] ?? modeStyles['off']
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
      style={{ background: s.bg, color: s.text }}
    >
      {s.label}
    </span>
  )
}
