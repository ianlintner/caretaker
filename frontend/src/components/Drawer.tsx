import { useEffect, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/cn'

export interface DrawerProps {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  width?: string
}

export default function Drawer({
  open,
  onClose,
  title,
  children,
  width = '560px',
}: DrawerProps) {
  // Close on Escape key
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, onClose])

  // Prevent body scroll when open
  useEffect(() => {
    if (open) {
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = ''
    }
    return () => {
      document.body.style.overflow = ''
    }
  }, [open])

  if (!open) return null

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/40 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Sheet */}
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          'fixed top-0 right-0 bottom-0 z-50',
          'flex flex-col',
          'bg-[var(--color-card-elevated)] border-l border-[var(--color-border)]',
          'shadow-2xl',
          'overflow-hidden',
        )}
        style={{ width }}
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-[var(--color-border)] shrink-0">
          <h2 className="text-base font-semibold text-[var(--color-foreground)] truncate">
            {title}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close drawer"
            className={cn(
              'shrink-0 p-1.5 rounded-md',
              'text-[var(--color-muted-foreground)]',
              'hover:text-[var(--color-foreground)] hover:bg-[var(--color-muted)]',
              'transition-colors',
            )}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Scrollable body */}
        <div className="flex-1 overflow-y-auto px-5 py-5 space-y-6">
          {children}
        </div>
      </aside>
    </>
  )
}
