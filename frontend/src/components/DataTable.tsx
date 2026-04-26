import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { cn } from '@/lib/cn'
import { nextSort } from '@/lib/tableSort'

export type SortDir = 'asc' | 'desc'

export type Column<T> = {
  key: string
  header: string
  width?: string
  render: (row: T) => ReactNode
  sortable?: boolean
  /**
   * Value used for sorting. Falls back to render output otherwise.
   * Return null/undefined to push to the bottom regardless of direction.
   */
  sortValue?: (row: T) => string | number | null | undefined
}

export type SortState = { key: string; dir: SortDir } | null

export function DataTable<T>({
  columns,
  rows,
  empty,
  onRowClick,
  sort,
  onSortChange,
}: {
  columns: Column<T>[]
  rows: T[]
  empty?: string
  onRowClick?: (row: T) => void
  sort?: SortState
  onSortChange?: (next: SortState) => void
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
              {columns.map((c) => {
                const isActive = sort?.key === c.key
                const sortable = c.sortable && onSortChange
                return (
                  <th
                    key={c.key}
                    style={c.width ? { width: c.width } : undefined}
                    className={cn(
                      'text-left px-4 py-2.5 font-medium',
                      'text-[11px] uppercase tracking-[0.08em]',
                      'text-[var(--color-muted-foreground)]',
                      'border-b border-[var(--color-border)]',
                      sortable && 'cursor-pointer select-none hover:text-[var(--color-foreground)]',
                    )}
                    onClick={
                      sortable
                        ? () => onSortChange!(nextSort(sort ?? null, c.key))
                        : undefined
                    }
                    aria-sort={
                      isActive
                        ? sort!.dir === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : sortable
                          ? 'none'
                          : undefined
                    }
                  >
                    <span className="inline-flex items-center gap-1">
                      {c.header}
                      {sortable &&
                        (isActive ? (
                          sort!.dir === 'asc' ? (
                            <ArrowUp className="h-3 w-3" />
                          ) : (
                            <ArrowDown className="h-3 w-3" />
                          )
                        ) : (
                          <ArrowUpDown className="h-3 w-3 opacity-40" />
                        ))}
                    </span>
                  </th>
                )
              })}
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
