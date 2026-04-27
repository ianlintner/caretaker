import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { cn } from '@/lib/cn'

export default function JsonViewer({
  data,
  maxHeight = '400px',
}: {
  data: unknown
  maxHeight?: string
}) {
  const [copied, setCopied] = useState(false)
  const json = JSON.stringify(data, null, 2)

  const handleCopy = () => {
    navigator.clipboard.writeText(json).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div
      className={cn(
        'relative rounded-md border border-[var(--color-border)]',
        'bg-[var(--color-muted)]/40',
        'overflow-hidden',
      )}
    >
      <button
        onClick={handleCopy}
        title="Copy JSON"
        aria-label="Copy JSON"
        className={cn(
          'absolute top-2 right-2 z-10',
          'p-1.5 rounded-md',
          'bg-[var(--color-card-elevated)] border border-[var(--color-border)]',
          'text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]',
          'transition-colors',
        )}
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-[var(--color-success)]" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>
      <pre
        className={cn(
          'text-[11px] font-mono p-4 pr-12 overflow-auto',
          'text-[var(--color-foreground)]',
          'whitespace-pre-wrap break-all',
        )}
        style={{ maxHeight }}
      >
        {json}
      </pre>
    </div>
  )
}
