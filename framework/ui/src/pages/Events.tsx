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
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <h1 className="text-xl font-bold">Events</h1>
        <div className="flex gap-2">
          <select
            value={refreshKey}
            onChange={(e) => setRefreshKey(e.target.value as 'manual' | '5s' | '10s' | '30s')}
            className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm"
          >
            {['5s', '10s', '30s', 'manual'].map(k => <option key={k}>{k}</option>)}
          </select>
          <button
            onClick={() => refresh(true)}
            className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm hover:bg-ink-700"
          >↻ reset</button>
        </div>
      </div>

      {error && (
        <div className="px-3 py-2 bg-glow-failure/10 border border-glow-failure/40 rounded text-sm text-glow-failure">
          {error}
        </div>
      )}

      <div className="bg-ink-800 rounded font-mono text-xs overflow-hidden">
        {events.length === 0 ? (
          <div className="p-4 text-ink-500 italic">No events recorded yet.</div>
        ) : (
          <div className="max-h-[70vh] overflow-y-auto">
            {events.slice().reverse().map((e, i) => (
              <div
                key={`${e.ts}-${i}`}
                className="grid grid-cols-[auto_120px_120px_1fr] gap-3 px-3 py-1.5 border-b border-ink-700/40 hover:bg-ink-700/30"
              >
                <span className="text-ink-500">{(e.ts || '').slice(0, 19)}</span>
                <span className="text-ink-300 truncate">{e.agent_id ?? '·'}</span>
                <span className={`${
                  e.state === 'running' ? 'text-glow-running' :
                  e.state === 'success' ? 'text-glow-success' :
                  e.state === 'failure' ? 'text-glow-failure' :
                  e.state === 'blocked' ? 'text-glow-blocked' :
                  'text-ink-400'
                }`}>{e.state ?? e.action ?? e.kind ?? '·'}</span>
                <span className="text-ink-300 truncate">{e.message ?? e.current_action ?? ''}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
