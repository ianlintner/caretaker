import { cn } from '@/lib/cn'

const STYLES: Record<string, string> = {
  open: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300',
  closed: 'bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300',
  merged: 'bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-300',
  draft: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
  success: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300',
  failed: 'bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300',
  running: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
}

export default function StatusBadge({ value }: { value: string }) {
  const key = value.toLowerCase()
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
        STYLES[key] || 'bg-[var(--color-muted)] text-[var(--color-muted-foreground)]',
      )}
    >
      {value}
    </span>
  )
}
