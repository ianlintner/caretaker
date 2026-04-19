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
    <div className="flex items-center justify-between pt-3 text-sm text-[var(--color-muted-foreground)]">
      <span>
        {total === 0 ? 0 : offset + 1}–{end} of {total}
      </span>
      <div className="flex items-center gap-1">
        <button
          disabled={!hasPrev}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="p-1.5 rounded-md border border-[var(--color-border)] disabled:opacity-40 hover:bg-[var(--color-muted)]"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <button
          disabled={!hasNext}
          onClick={() => onChange(offset + limit)}
          className="p-1.5 rounded-md border border-[var(--color-border)] disabled:opacity-40 hover:bg-[var(--color-muted)]"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
