import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

export type Column<T> = {
  key: string
  header: string
  width?: string
  render: (row: T) => ReactNode
}

export function DataTable<T>({
  columns,
  rows,
  empty,
  onRowClick,
}: {
  columns: Column<T>[]
  rows: T[]
  empty?: string
  onRowClick?: (row: T) => void
}) {
  if (rows.length === 0) {
    return (
      <div className="panel p-10 text-center text-sm text-[var(--color-muted-foreground)]">
        {empty || 'No data.'}
      </div>
    )
  }

  return (
    <div className="panel overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm tnum">
          <thead className="bg-[var(--color-muted)]/60 backdrop-blur-sm sticky top-0 z-[1]">
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  style={c.width ? { width: c.width } : undefined}
                  className={cn(
                    'text-left px-4 py-2.5 font-medium',
                    'text-[11px] uppercase tracking-[0.08em]',
                    'text-[var(--color-muted-foreground)]',
                    'border-b border-[var(--color-border)]',
                  )}
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr
                key={i}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={cn(
                  'group border-t border-[var(--color-border)]',
                  'transition-colors duration-[var(--motion-fast)]',
                  onRowClick &&
                    'cursor-pointer hover:bg-[var(--color-primary-soft)]',
                )}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={cn(
                      'px-4 py-2.5 align-middle',
                      'text-[var(--color-foreground)]',
                    )}
                  >
                    {c.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
