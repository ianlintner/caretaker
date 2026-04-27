import type { ReactNode } from 'react'

export function FilterBar({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      {children}
    </div>
  )
}

export function FilterSelect<T extends string>({
  label,
  value,
  options,
  onChange,
  placeholder,
}: {
  label?: string
  value: T | ''
  options: { value: T; label: string }[]
  onChange: (next: T | '') => void
  placeholder?: string
}) {
  return (
    <label className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
      {label && <span>{label}</span>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as T | '')}
        className="text-sm border border-[var(--color-border)] rounded-md px-2.5 py-1.5 bg-[var(--color-card)] text-[var(--color-foreground)]"
      >
        <option value="">{placeholder ?? 'All'}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  )
}

export function FilterToggle({
  active,
  onChange,
  children,
}: {
  active: boolean
  onChange: (next: boolean) => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!active)}
      className="px-2.5 py-1 rounded-md border text-xs transition-colors"
      style={{
        borderColor: active
          ? 'var(--color-primary)'
          : 'var(--color-border)',
        background: active ? 'var(--color-primary-soft)' : 'transparent',
        color: active ? 'var(--color-foreground)' : 'var(--color-muted-foreground)',
      }}
      aria-pressed={active}
    >
      {children}
    </button>
  )
}
