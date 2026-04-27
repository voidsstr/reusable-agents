// Agent grid — light-theme. Hero section spotlights currently-running agents,
// then filter pills, then the full grid. Pattern inspired by Linear's project
// list + Vercel's deployments dashboard.

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
  const [statusFilter, setStatusFilter] = useState<string>(
    () => localStorage.getItem('agent-list-status-filter') || 'all'
  )
  const [search, setSearch] = useState('')

  useEffect(() => { localStorage.setItem('agent-list-filter', filter) }, [filter])
  useEffect(() => { localStorage.setItem('agent-list-app-filter', appFilter) }, [appFilter])
  useEffect(() => { localStorage.setItem('agent-list-status-filter', statusFilter) }, [statusFilter])

  const [refreshKey, setRefreshKey] = useState<keyof typeof REFRESH_INTERVALS_MS>('10s')
  const [error, setError] = useState<string>('')
  const [loading, setLoading] = useState(true)

  const wsRefs = useRef<Map<string, WebSocket>>(new Map())

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

  // WebSockets — one per agent. Open for ALL agents (not just enabled) so
  // disabled agents that get manually triggered still glow live.
  useEffect(() => {
    const wantedIds = new Set(agents.map(a => a.id))
    for (const [id, ws] of wsRefs.current) {
      if (!wantedIds.has(id)) {
        ws.close()
        wsRefs.current.delete(id)
      }
    }
    for (const id of wantedIds) {
      if (wsRefs.current.has(id)) continue
      const ws = openStatusWS(id, (status) => {
        setStatuses(s => ({ ...s, [id]: status }))
      })
      if (ws) wsRefs.current.set(id, ws)
    }
    return () => {
      for (const ws of wsRefs.current.values()) ws.close()
      wsRefs.current.clear()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents.length, agents.map(a => a.id).join(',')])

  const categories = useMemo(() => {
    const set = new Set(agents.map(a => a.category))
    return ['all', ...Array.from(set).sort()]
  }, [agents])

  const applications = useMemo(() => {
    const set = new Set(agents.map(a => a.application || 'shared'))
    return ['all', ...Array.from(set).sort()]
  }, [agents])

  const effectiveState = (a: AgentSummary): string => {
    return statuses[a.id]?.state ?? a.last_run_status ?? ''
  }

  const filtered = useMemo(() => {
    return agents.filter(a => {
      if (filter !== 'all' && a.category !== filter) return false
      if (appFilter !== 'all' && (a.application || 'shared') !== appFilter) return false
      if (statusFilter !== 'all') {
        const s = effectiveState(a)
        if (statusFilter === 'active' && !(s === 'running' || s === 'starting')) return false
        if (statusFilter !== 'active' && s !== statusFilter) return false
      }
      if (search) {
        const q = search.toLowerCase()
        if (!`${a.name} ${a.id} ${a.description}`.toLowerCase().includes(q)) return false
      }
      return true
    })
  }, [agents, filter, appFilter, statusFilter, search, statuses])

  const statusCounts = useMemo(() => {
    const c: Record<string, number> = { all: agents.length, active: 0 }
    for (const a of agents) {
      const s = effectiveState(a)
      c[s] = (c[s] || 0) + 1
      if (s === 'running' || s === 'starting') c.active += 1
    }
    return c
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, statuses])

  // Currently-running agents go into a hero section at the top
  const runningAgents = useMemo(
    () => agents.filter(a => {
      const s = effectiveState(a)
      return s === 'running' || s === 'starting'
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [agents, statuses],
  )

  const triggerOne = async (id: string) => {
    try {
      await api.triggerAgent(id)
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
    <div className="space-y-5">
      {/* Header — page title + summary metrics + global actions */}
      <div className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900 tracking-tight">Agents</h1>
          <div className="text-sm text-ink-500 mt-0.5">
            <span className="font-medium text-ink-700">{agents.length}</span> registered
            <span className="mx-2 text-ink-300">·</span>
            <span className="font-medium text-ink-700">{agents.filter(a => a.enabled).length}</span> enabled
            <span className="mx-2 text-ink-300">·</span>
            <span className="font-medium text-status-running-fg">
              <span className="status-dot inline-block mr-1" style={{ ['--glow-color' as string]: '59 130 246' }} />
              {runningAgents.length}
            </span> running now
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search agents…"
            className="px-3 py-1.5 bg-surface-card border border-surface-divider rounded-lg text-sm w-56 placeholder:text-ink-400 focus:outline-none focus:border-accent-500 focus:ring-2 focus:ring-accent-500/20"
          />
          <select
            value={refreshKey}
            onChange={(e) => setRefreshKey(e.target.value as keyof typeof REFRESH_INTERVALS_MS)}
            className="px-3 py-1.5 bg-surface-card border border-surface-divider rounded-lg text-sm text-ink-700 focus:outline-none focus:border-accent-500"
          >
            {Object.keys(REFRESH_INTERVALS_MS).map(k => (
              <option key={k} value={k}>refresh {k}</option>
            ))}
          </select>
          <button
            onClick={refresh}
            className="btn-secondary"
            title="Refresh now"
          >↻</button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2.5 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {/* Hero — currently-working agents */}
      {runningAgents.length > 0 && (
        <div className="bg-gradient-to-r from-accent-50 to-status-starting-bg/30 border border-accent-100 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xl animate-subtle-spin inline-block">⚙️</span>
            <h2 className="text-sm font-semibold text-ink-900 uppercase tracking-wide">
              Working now <span className="text-ink-400 font-normal">({runningAgents.length})</span>
            </h2>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {runningAgents.map(a => (
              <AgentCard
                key={a.id}
                agent={a}
                status={statuses[a.id]}
                onTrigger={triggerOne}
                onToggleEnabled={toggleEnabled}
              />
            ))}
          </div>
        </div>
      )}

      {/* Filters — three rows of pills, grouped */}
      <div className="space-y-3 bg-surface-card border border-surface-divider rounded-xl p-4 shadow-card">
        <FilterRow label="Application" value={appFilter} onChange={setAppFilter} options={applications.map(app => ({
          key: app,
          label: app,
          icon: APPLICATION_EMOJI[app] || '',
          count: app === 'all' ? agents.length : agents.filter(a => (a.application || 'shared') === app).length,
        }))} testIdPrefix="app-filter" />

        <FilterRow label="Category" value={filter} onChange={setFilter} options={categories.map(cat => ({
          key: cat,
          label: cat,
          count: cat === 'all' ? agents.length : agents.filter(a => a.category === cat).length,
        }))} />

        <FilterRow label="Status" value={statusFilter} onChange={setStatusFilter} options={[
          { key: 'all',     label: 'all',           count: statusCounts.all || 0 },
          { key: 'active',  label: '⚙ working now', count: statusCounts.active || 0, accent: 'running' },
          { key: 'success', label: '✓ success',     count: statusCounts.success || 0, accent: 'success' },
          { key: 'failure', label: '✗ failure',     count: statusCounts.failure || 0, accent: 'failure' },
          { key: 'blocked', label: '⏸ blocked',     count: statusCounts.blocked || 0, accent: 'blocked' },
          { key: 'idle',    label: '· idle',        count: statusCounts.idle || 0 },
        ]} testIdPrefix="status-filter" />
      </div>

      {/* Main grid */}
      {loading ? (
        <div className="text-center py-12 text-ink-400">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-ink-400">
          No agents{search ? ` matching "${search}"` : ''}.
        </div>
      ) : (
        <div>
          <div className="text-xs text-ink-400 mb-2 px-1">
            {filtered.length === agents.length
              ? `${agents.length} agents`
              : `${filtered.length} of ${agents.length} agents`}
          </div>
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
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------

interface FilterOption {
  key: string
  label: string
  icon?: string
  count?: number
  accent?: 'running' | 'success' | 'failure' | 'blocked'
}

function FilterRow({ label, value, onChange, options, testIdPrefix }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: FilterOption[]
  testIdPrefix?: string
}) {
  return (
    <div>
      <div className="text-[10px] uppercase text-ink-400 font-semibold tracking-wider mb-1.5">{label}</div>
      <div className="flex gap-1.5 flex-wrap">
        {options.map(o => {
          const isActive = value === o.key
          const accent = o.accent
          let activeStyles = 'bg-accent-50 text-accent-700 border-accent-500'
          if (accent === 'running') activeStyles = 'bg-status-running-bg text-status-running-fg border-status-running-glow ring-2 ring-status-running-glow/20'
          else if (accent === 'success') activeStyles = 'bg-status-success-bg text-status-success-fg border-status-success-glow'
          else if (accent === 'failure') activeStyles = 'bg-status-failure-bg text-status-failure-fg border-status-failure-glow'
          else if (accent === 'blocked') activeStyles = 'bg-status-blocked-bg text-status-blocked-fg border-status-blocked-glow'
          return (
            <button
              key={o.key}
              data-testid={testIdPrefix ? `${testIdPrefix}-${o.key}` : undefined}
              onClick={() => onChange(o.key)}
              className={`text-xs px-3 py-1 rounded-full border transition-all font-medium ${
                isActive
                  ? activeStyles
                  : 'bg-surface-card text-ink-600 border-surface-divider hover:bg-surface-subtle hover:border-ink-300'
              }`}
            >
              {o.icon && <span className="mr-1">{o.icon}</span>}
              {o.label}
              {o.count !== undefined && (
                <span className={`ml-1.5 ${isActive ? 'opacity-70' : 'text-ink-400'}`}>{o.count}</span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
