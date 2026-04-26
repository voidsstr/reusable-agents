// Agent grid with live-status WebSocket push + filter pills + auto-refresh.

import { useEffect, useMemo, useRef, useState } from 'react'
import { api, openStatusWS } from '../api/client'
import type { AgentLiveStatus, AgentSummary } from '../api/types'
import AgentCard from '../components/AgentCard'

const REFRESH_INTERVALS_MS: Record<string, number> = {
  '5s': 5_000, '10s': 10_000, '30s': 30_000, '60s': 60_000, 'manual': 0,
}

const APPLICATION_EMOJI: Record<string, string> = {
  'all': '🗂',
  'aisleprompt': '🛒',
  'specpicks': '🎮',
  'reusable-agents': '🔧',
  'seo-pipeline': '🎯',
  'retro-fleet': '🖥',
  'personal': '📅',
  'shared': '🔌',
  'research': '🔬',
  'ops': '⚙️',
}

export default function AgentList() {
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [statuses, setStatuses] = useState<Record<string, AgentLiveStatus>>({})
  const [filter, setFilter] = useState<string>(
    () => localStorage.getItem('agent-list-filter') || 'all'
  )
  const [appFilter, setAppFilter] = useState<string>(
    () => localStorage.getItem('agent-list-app-filter') || 'all'
  )
  const [search, setSearch] = useState('')

  // Persist filter selections per browser
  useEffect(() => { localStorage.setItem('agent-list-filter', filter) }, [filter])
  useEffect(() => { localStorage.setItem('agent-list-app-filter', appFilter) }, [appFilter])
  const [refreshKey, setRefreshKey] = useState<keyof typeof REFRESH_INTERVALS_MS>('10s')
  const [error, setError] = useState<string>('')
  const [loading, setLoading] = useState(true)

  const wsRefs = useRef<Map<string, WebSocket>>(new Map())

  // Initial + interval REST refresh
  const refresh = async () => {
    try {
      const list = await api.listAgents()
      setAgents(list)
      setError('')
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [])

  useEffect(() => {
    const ms = REFRESH_INTERVALS_MS[refreshKey] || 0
    if (!ms) return
    const id = setInterval(refresh, ms)
    return () => clearInterval(id)
  }, [refreshKey])

  // WebSockets for live status — one per agent (only for enabled agents to save sockets)
  useEffect(() => {
    const wantedIds = new Set(agents.filter(a => a.enabled).map(a => a.id))
    // Close WS for agents we no longer track
    for (const [id, ws] of wsRefs.current) {
      if (!wantedIds.has(id)) {
        ws.close()
        wsRefs.current.delete(id)
      }
    }
    // Open WS for new agents
    for (const id of wantedIds) {
      if (wsRefs.current.has(id)) continue
      const ws = openStatusWS(id, (status) => {
        setStatuses(s => ({ ...s, [id]: status }))
      })
      if (ws) wsRefs.current.set(id, ws)
    }
    return () => {
      // Cleanup on unmount
      for (const ws of wsRefs.current.values()) ws.close()
      wsRefs.current.clear()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents.length, agents.map(a => a.id + a.enabled).join(',')])

  const categories = useMemo(() => {
    const set = new Set(agents.map(a => a.category))
    return ['all', ...Array.from(set).sort()]
  }, [agents])

  const applications = useMemo(() => {
    const set = new Set(agents.map(a => a.application || 'shared'))
    return ['all', ...Array.from(set).sort()]
  }, [agents])

  const filtered = useMemo(() => {
    return agents.filter(a => {
      if (filter !== 'all' && a.category !== filter) return false
      if (appFilter !== 'all' && (a.application || 'shared') !== appFilter) return false
      if (search) {
        const q = search.toLowerCase()
        if (!`${a.name} ${a.id} ${a.description}`.toLowerCase().includes(q)) return false
      }
      return true
    })
  }, [agents, filter, appFilter, search])

  const triggerOne = async (id: string) => {
    try {
      await api.triggerAgent(id)
      // Optimistic — let the WS catch the running state push
      setTimeout(refresh, 1500)
    } catch (e) {
      alert(`Trigger failed: ${e}`)
    }
  }

  const toggleEnabled = async (a: AgentSummary) => {
    try {
      if (a.enabled) await api.disableAgent(a.id)
      else await api.enableAgent(a.id)
      await refresh()
    } catch (e) {
      alert(`Toggle failed: ${e}`)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold">Agents</h1>
          <div className="text-sm text-ink-400">
            {agents.length} registered · {agents.filter(a => a.enabled).length} enabled · {' '}
            {Object.values(statuses).filter(s => s?.state === 'running').length} running now
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search…"
            className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm w-48"
          />
          <select
            value={refreshKey}
            onChange={(e) => setRefreshKey(e.target.value as keyof typeof REFRESH_INTERVALS_MS)}
            className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm"
          >
            {Object.keys(REFRESH_INTERVALS_MS).map(k => (
              <option key={k} value={k}>refresh {k}</option>
            ))}
          </select>
          <button
            onClick={refresh}
            className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm hover:bg-ink-700"
          >↻</button>
        </div>
      </div>

      {error && (
        <div className="px-3 py-2 bg-glow-failure/10 border border-glow-failure/40 rounded text-sm text-glow-failure">
          {error}
        </div>
      )}

      {/* Application filter row */}
      <div>
        <div className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide mb-1.5">Application</div>
        <div className="flex gap-1.5 flex-wrap">
          {applications.map(app => {
            const count = app === 'all'
              ? agents.length
              : agents.filter(a => (a.application || 'shared') === app).length
            const emoji = APPLICATION_EMOJI[app] || ''
            return (
              <button
                key={app}
                data-testid={`app-filter-${app}`}
                onClick={() => setAppFilter(app)}
                className={`text-xs px-3 py-1 rounded-full transition-colors ${
                  appFilter === app
                    ? 'bg-glow-running/20 text-glow-running border border-glow-running/40'
                    : 'bg-ink-800/50 text-ink-300 border border-ink-700 hover:bg-ink-800'
                }`}
              >
                {emoji && <span className="mr-1">{emoji}</span>}
                {app}
                <span className="ml-1.5 text-ink-500">{count}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* Category filter row */}
      <div>
        <div className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide mb-1.5">Category</div>
        <div className="flex gap-1.5 flex-wrap">
          {categories.map(cat => (
            <button
              key={cat}
              onClick={() => setFilter(cat)}
              className={`text-xs px-3 py-1 rounded-full transition-colors ${
                filter === cat
                  ? 'bg-ink-700 text-ink-50 border border-ink-600'
                  : 'bg-ink-800/50 text-ink-400 border border-ink-700 hover:bg-ink-800'
              }`}
            >
              {cat}
              {cat !== 'all' && (
                <span className="ml-1.5 text-ink-500">{agents.filter(a => a.category === cat).length}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-12 text-ink-500">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-ink-500">
          No agents{search ? ` matching "${search}"` : ''}.
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {filtered.map(a => (
            <AgentCard
              key={a.id}
              agent={a}
              status={statuses[a.id]}
              onTrigger={triggerOne}
              onToggleEnabled={toggleEnabled}
            />
          ))}
        </div>
      )}
    </div>
  )
}
