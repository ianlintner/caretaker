import { useEffect, useRef, useState } from 'react'
import { Search, X } from 'lucide-react'

/**
 * Debounced search input.
 *
 * Local input state is initialized from `value` once. Subsequent external
 * resets should be done by changing the parent's `key` so the component
 * remounts — that avoids React 19's set-state-in-effect rule and keeps
 * uncontrolled typing snappy.
 */
export default function SearchInput({
  value,
  onChange,
  placeholder = 'Search…',
  debounceMs = 200,
  width = '14rem',
}: {
  value: string
  onChange: (next: string) => void
  placeholder?: string
  debounceMs?: number
  width?: string
}) {
  const [local, setLocal] = useState(value)
  const lastEmitted = useRef(value)

  useEffect(() => {
    if (local === lastEmitted.current) return
    const t = setTimeout(() => {
      lastEmitted.current = local
      onChange(local)
    }, debounceMs)
    return () => clearTimeout(t)
  }, [local, debounceMs, onChange])

  return (
    <div className="relative inline-flex items-center" style={{ width }}>
      <Search
        className="h-3.5 w-3.5 absolute left-2.5 text-[var(--color-muted-foreground)] pointer-events-none"
        aria-hidden
      />
      <input
        type="search"
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        placeholder={placeholder}
        className="w-full text-sm border border-[var(--color-border)] rounded-md pl-8 pr-7 py-1.5 bg-[var(--color-card)] focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)]/30"
      />
      {local && (
        <button
          type="button"
          onClick={() => {
            setLocal('')
            lastEmitted.current = ''
            onChange('')
          }}
          aria-label="Clear search"
          className="absolute right-1.5 p-0.5 rounded text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}
