import { useEffect, useRef, useState } from 'react'
import { Monitor, Moon, Sun, Check } from 'lucide-react'
import { useTheme, type ThemeMode } from '@/hooks/useTheme'
import { cn } from '@/lib/cn'

const OPTIONS: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
]

export default function ThemeToggle() {
  const { mode, resolved, setMode } = useTheme()
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const Current = mode === 'system' ? Monitor : resolved === 'dark' ? Moon : Sun

  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        aria-label={`Theme: ${mode}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex items-center justify-center h-8 w-8 rounded-md',
          'border border-[var(--color-border)] bg-[var(--color-surface)]',
          'text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]',
          'hover:border-[var(--color-border-strong)] transition-colors',
        )}
      >
        <Current className="h-4 w-4" />
      </button>
      {open && (
        <div
          role="menu"
          className={cn(
            'absolute right-0 mt-2 w-40 z-30',
            'rounded-[var(--radius-md)] border border-[var(--color-border)]',
            'bg-[var(--color-card-elevated)] shadow-[var(--shadow-lg)] p-1',
          )}
        >
          {OPTIONS.map(({ value, label, icon: Icon }) => {
            const active = mode === value
            return (
              <button
                key={value}
                role="menuitemradio"
                aria-checked={active}
                onClick={() => {
                  setMode(value)
                  setOpen(false)
                }}
                className={cn(
                  'flex items-center gap-2 w-full px-2.5 py-1.5 rounded-[var(--radius-sm)] text-sm text-left',
                  'hover:bg-[var(--color-muted)] transition-colors',
                  active && 'text-[var(--color-foreground)]',
                )}
              >
                <Icon className="h-4 w-4 text-[var(--color-muted-foreground)]" />
                <span className="flex-1">{label}</span>
                {active && <Check className="h-3.5 w-3.5 text-[var(--color-primary)]" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
