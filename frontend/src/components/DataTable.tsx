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
      <div className="border border-[var(--color-border)] rounded-lg p-8 text-center text-sm text-[var(--color-muted-foreground)]">
        {empty || 'No data.'}
      </div>
    )
  }

  return (
    <div className="border border-[var(--color-border)] rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-[var(--color-muted)]">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                style={c.width ? { width: c.width } : undefined}
                className="text-left px-4 py-2.5 font-medium text-[var(--color-muted-foreground)]"
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
                'border-t border-[var(--color-border)]',
                onRowClick && 'cursor-pointer hover:bg-[var(--color-muted)]',
              )}
            >
              {columns.map((c) => (
                <td key={c.key} className="px-4 py-2.5 align-middle">
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
