type SiteDay = { site: string; day: string; rate: number }

function rateColor(rate: number): string {
  if (rate >= 0.9) return 'var(--color-success)'
  if (rate >= 0.75) return 'rgba(34,197,94,0.5)'
  if (rate >= 0.6) return 'var(--color-warning)'
  return 'var(--color-destructive)'
}

export default function AgreementHeatmap({
  data,
  sites,
  days,
}: {
  data: SiteDay[]
  sites: string[]
  days: string[]
}) {
  const lookup = new Map(data.map((d) => [`${d.site}:${d.day}`, d.rate]))

  return (
    <div className="overflow-x-auto">
      <table className="text-[11px] border-separate border-spacing-0.5">
        <thead>
          <tr>
            <th className="text-left text-[var(--color-muted-foreground)] pr-2 font-normal w-32">Site</th>
            {days.map((d) => (
              <th key={d} className="text-[var(--color-muted-foreground)] font-normal px-1 min-w-[32px]">
                {d}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sites.map((site) => (
            <tr key={site}>
              <td className="text-[var(--color-foreground)] pr-2 py-0.5 truncate max-w-[128px]" title={site}>
                {site}
              </td>
              {days.map((day) => {
                const rate = lookup.get(`${site}:${day}`)
                return (
                  <td key={day} className="px-1 py-0.5 text-center">
                    {rate !== undefined ? (
                      <span
                        className="inline-block w-7 h-5 rounded-[2px] text-[10px] leading-5"
                        style={{ background: rateColor(rate), color: 'white' }}
                        title={`${(rate * 100).toFixed(0)}%`}
                      >
                        {(rate * 100).toFixed(0)}
                      </span>
                    ) : (
                      <span className="inline-block w-7 h-5 rounded-[2px] bg-[var(--color-muted)]" />
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
