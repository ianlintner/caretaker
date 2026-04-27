import { ChevronLeft, ChevronRight } from 'lucide-react'

export default function Pagination({
  offset,
  limit,
  total,
  onChange,
}: {
  offset: number
  limit: number
  total: number
  onChange: (next: number) => void
}) {
  const end = Math.min(offset + limit, total)
  const hasPrev = offset > 0
  const hasNext = end < total
  return (
    <div className="flex items-center justify-between pt-3 text-xs text-[var(--color-muted-foreground)]">
      <span className="mono">
        {total === 0 ? 0 : offset + 1}–{end} of {total}
      </span>
      <div className="flex items-center gap-1">
        <button
          aria-label="Previous page"
          disabled={!hasPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="p-1.5 rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-surface)] disabled:opacity-40 hover:bg-[var(--color-muted)] hover:border-[var(--color-border-strong)] transition-colors"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <button
          aria-label="Next page"
          disabled={!hasNext}
          onClick={() => onChange(offset + limit)}
          className="p-1.5 rounded-[var(--radius-sm)] border border-[var(--color-border)] bg-[var(--color-surface)] disabled:opacity-40 hover:bg-[var(--color-muted)] hover:border-[var(--color-border-strong)] transition-colors"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
