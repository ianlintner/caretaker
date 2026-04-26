import type { Column, SortState } from '@/components/DataTable'

function compare(
  a: string | number | null | undefined,
  b: string | number | null | undefined,
): number {
  const aNull = a === null || a === undefined
  const bNull = b === null || b === undefined
  if (aNull && bNull) return 0
  if (aNull) return 1
  if (bNull) return -1
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b))
}

export function sortRows<T>(
  rows: T[],
  columns: Column<T>[],
  sort: SortState,
): T[] {
  if (!sort) return rows
  const col = columns.find((c) => c.key === sort.key)
  if (!col || !col.sortable || !col.sortValue) return rows
  const dir = sort.dir === 'asc' ? 1 : -1
  const indexed = rows.map((row, i) => ({ row, i }))
  indexed.sort((x, y) => {
    const cmp = compare(col.sortValue!(x.row), col.sortValue!(y.row))
    if (cmp !== 0) return cmp * dir
    return x.i - y.i
  })
  return indexed.map((x) => x.row)
}

export function nextSort(current: SortState, key: string): SortState {
  if (!current || current.key !== key) return { key, dir: 'asc' }
  if (current.dir === 'asc') return { key, dir: 'desc' }
  return null
}
