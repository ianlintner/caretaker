import type { ReactNode } from 'react'

export default function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-[var(--color-border)] px-6 py-5">
      <div className="relative pl-3">
        <span
          aria-hidden
          className="absolute left-0 top-1 bottom-1 w-[3px] rounded-full"
          style={{
            background:
              'linear-gradient(180deg, var(--color-primary), var(--color-accent))',
          }}
        />
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && (
          <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
            {description}
          </p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  )
}
