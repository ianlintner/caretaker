import useSWR from 'swr'
import PageHeader from '@/components/PageHeader'
import type { AgentInfo } from '@/lib/types'

export default function Agents() {
  const { data, isLoading } = useSWR<AgentInfo[]>('/api/admin/agents')

  return (
    <>
      <PageHeader
        title="Agents"
        description="Registered agents, their modes, and event triggers."
      />
      <div className="p-8">
        {isLoading ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">Loading…</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {(data ?? []).map((agent) => (
              <div
                key={agent.name}
                className="border border-[var(--color-border)] rounded-lg p-4 bg-[var(--color-card)]"
              >
                <h3 className="font-medium text-sm">{agent.name}</h3>
                <div className="mt-3 space-y-2">
                  <div>
                    <p className="text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
                      Modes
                    </p>
                    <div className="flex flex-wrap gap-1 mt-1">
                      {agent.modes.map((m) => (
                        <span
                          key={m}
                          className="px-1.5 py-0.5 text-[11px] rounded bg-[var(--color-muted)]"
                        >
                          {m}
                        </span>
                      ))}
                    </div>
                  </div>
                  {agent.events.length > 0 && (
                    <div>
                      <p className="text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">
                        Events
                      </p>
                      <div className="flex flex-wrap gap-1 mt-1">
                        {agent.events.map((e) => (
                          <span
                            key={e}
                            className="px-1.5 py-0.5 text-[11px] rounded bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-300"
                          >
                            {e}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}
