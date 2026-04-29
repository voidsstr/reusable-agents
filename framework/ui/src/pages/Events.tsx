// Live framework event log — auto-tails as new events arrive.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { FrameworkEvent } from '../api/types'

export default function Events() {
  const [events, setEvents] = useState<FrameworkEvent[]>([])
  const [refreshKey, setRefreshKey] = useState<'5s' | '10s' | '30s' | 'manual'>('5s')
  const [error, setError] = useState('')
  const cursorRef = useRef('')

  const refresh = async (reset = false) => {
    try {
      const since = reset ? undefined : (cursorRef.current || undefined)
      const fresh = await api.events(since, 200)
      if (reset) {
        setEvents(fresh)
      } else if (fresh.length) {
        setEvents(prev => [...prev, ...fresh].slice(-500))
      }
      if (fresh.length) cursorRef.current = fresh[fresh.length - 1].ts ?? cursorRef.current
      setError('')
    } catch (e) {
      setError(String(e))
    }
  }

  useEffect(() => { void refresh(true) }, [])

  useEffect(() => {
    const ms = { '5s': 5000, '10s': 10000, '30s': 30000, manual: 0 }[refreshKey]
    if (!ms) return
    const id = setInterval(() => refresh(false), ms)
    return () => clearInterval(id)
  }, [refreshKey])

  return (
    <div className="space-y-3">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <h1 className="text-xl sm:text-2xl font-bold">Events</h1>
        <div className="flex gap-2">
          <select
            value={refreshKey}
            onChange={(e) => setRefreshKey(e.target.value as 'manual' | '5s' | '10s' | '30s')}
            className="px-3 py-1.5 bg-surface-card border border-surface-divider rounded-md text-sm"
          >
            {['5s', '10s', '30s', 'manual'].map(k => <option key={k}>{k}</option>)}
          </select>
          <button
            onClick={() => refresh(true)}
            className="px-3 py-1.5 bg-surface-card border border-surface-divider rounded-md text-sm hover:bg-surface-subtle"
          >↻ reset</button>
        </div>
      </div>

      {error && (
        <div className="px-3 py-2 bg-status-failure-bg border border-status-failure-glow/40 rounded-md text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      <div className="bg-surface-card border border-surface-divider rounded-lg font-mono text-xs overflow-hidden shadow-card">
        {events.length === 0 ? (
          <div className="p-4 text-ink-500 italic">No events recorded yet.</div>
        ) : (
          <div className="max-h-[70vh] overflow-y-auto divide-y divide-surface-divider">
            {events.slice().reverse().map((e, i) => {
              const stateClass =
                e.state === 'running' ? 'text-status-running-fg' :
                e.state === 'success' ? 'text-status-success-fg' :
                e.state === 'failure' ? 'text-status-failure-fg' :
                e.state === 'blocked' ? 'text-status-blocked-fg' :
                'text-ink-400'
              return (
                <div
                  key={`${e.ts}-${i}`}
                  className="px-3 py-2 hover:bg-surface-subtle"
                >
                  {/* Mobile: 2-line stacked layout. Desktop: 4-col grid. */}
                  <div className="hidden md:grid md:grid-cols-[auto_140px_120px_1fr] md:gap-3 md:items-baseline">
                    <span className="text-ink-500">{(e.ts || '').slice(0, 19)}</span>
                    <span className="text-ink-600 truncate">{e.agent_id ?? '·'}</span>
                    <span className={stateClass}>{e.state ?? e.action ?? e.kind ?? '·'}</span>
                    <span className="text-ink-600 truncate">{e.message ?? e.current_action ?? ''}</span>
                  </div>
                  <div className="md:hidden">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-ink-600 font-semibold truncate text-[11px]">{e.agent_id ?? '·'}</span>
                      <span className={`${stateClass} text-[10px] uppercase tracking-wide font-semibold shrink-0`}>{e.state ?? e.action ?? e.kind ?? '·'}</span>
                    </div>
                    <div className="text-ink-500 text-[10px] mt-0.5">{(e.ts || '').slice(0, 19)}</div>
                    {(e.message || e.current_action) && (
                      <div className="text-ink-700 mt-1 break-words">{e.message ?? e.current_action}</div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
