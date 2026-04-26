import { cn } from '@/lib/cn'

type DotTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

const TONE_CLASSES: Record<DotTone, string> = {
  success: 'bg-[var(--color-success)]',
  warning: 'bg-[var(--color-warning)]',
  danger: 'bg-[var(--color-destructive)]',
  info: 'bg-[var(--color-primary)]',
  neutral: 'bg-[var(--color-muted-foreground)]',
}

export default function StatusDot({
  tone = 'neutral',
  pulse = false,
  className,
}: {
  tone?: DotTone
  pulse?: boolean
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-block h-2 w-2 rounded-full',
        TONE_CLASSES[tone],
        pulse && 'animate-pulse',
        className,
      )}
    />
  )
}
