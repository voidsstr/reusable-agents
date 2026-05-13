// Settings — schedule management for all registered agents.
// Each row: agent meta + a cadence dropdown (1h, 2h, 5h, 12h, daily, custom, disabled).
// Top action: "Apply 5h cadence to all enabled" — bulk update with staggered minute offsets.

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { AgentSummary } from '../api/types'

type Cadence = 'disabled' | '1h' | '2h' | '5h' | '12h' | 'daily' | 'custom'

const CADENCE_LABELS: Record<Cadence, string> = {
  disabled: 'Disabled',
  '1h':     'Every hour',
  '2h':     'Every 2 hours',
  '5h':     'Every 5 hours',
  '12h':    'Every 12 hours',
  daily:    'Once a day (06:00)',
  custom:   'Custom (keep)',
}

const CADENCE_ORDER: Cadence[] = ['disabled', '1h', '2h', '5h', '12h', 'daily', 'custom']

// Map a cron string to one of our preset cadences.
function classifyCron(cron: string): Cadence {
  const c = (cron || '').trim()
  if (!c) return 'disabled'
  // Match "<min> * * * *" → 1h
  if (/^\d{1,2}\s+\*\s+\*\s+\*\s+\*$/.test(c)) return '1h'
  if (/^\d{1,2}\s+\*\/2\s+\*\s+\*\s+\*$/.test(c)) return '2h'
  if (/^\d{1,2}\s+\*\/5\s+\*\s+\*\s+\*$/.test(c)) return '5h'
  if (/^\d{1,2}\s+\*\/12\s+\*\s+\*\s+\*$/.test(c)) return '12h'
  if (/^\d{1,2}\s+\d{1,2}\s+\*\s+\*\s+\*$/.test(c)) return 'daily'
  return 'custom'
}

// Build a cron expression from a preset, given a minute offset (0-59).
// We keep a single hour anchor (06) for daily; the */N forms stay aligned.
function buildCron(target: Cadence, minuteOffset: number): string | null {
  const m = ((minuteOffset % 60) + 60) % 60
  switch (target) {
    case 'disabled': return ''
    case '1h':       return `${m} * * * *`
    case '2h':       return `${m} */2 * * *`
    case '5h':       return `${m} */5 * * *`
    case '12h':      return `${m} */12 * * *`
    case 'daily':    return `${m} 6 * * *`
    case 'custom':   return null  // signals "do not change"
  }
}

// Existing minute-offset extractor — preserves what the user already had if possible.
function existingMinuteOffset(cron: string): number {
  const m = (cron || '').trim().match(/^(\d{1,2})\s/)
  if (!m) return 0
  const n = parseInt(m[1], 10)
  return Number.isFinite(n) && n >= 0 && n < 60 ? n : 0
}

type SortKey = 'id' | 'name' | 'category' | 'application' | 'cron' | 'enabled' | 'cadence'

export default function Settings() {
  const [agents, setAgents]   = useState<AgentSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState<string | null>(null)
  const [saving, setSaving]   = useState<Record<string, boolean>>({})
  const [pending, setPending] = useState<Record<string, Cadence>>({})
  const [search, setSearch]   = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)
  const [flash, setFlash]     = useState<string | null>(null)

  // Filters — null/'all' = include everything for that dimension
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [appFilter, setAppFilter]           = useState<string>('all')
  const [cadenceFilter, setCadenceFilter]   = useState<string>('all')
  const [enabledFilter, setEnabledFilter]   = useState<'all' | 'enabled' | 'disabled'>('all')

  // Sort: null = default alphabetical by id
  const [sortKey, setSortKey] = useState<SortKey | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const toggleSort = (k: SortKey) => {
    if (sortKey === k) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(k); setSortDir('asc')
    }
  }

  async function loadAgents() {
    setLoading(true)
    try {
      const list = await api.listAgents()
      setAgents(list)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { loadAgents() }, [])

  // Distinct values for filter dropdowns
  const categories = useMemo(
    () => Array.from(new Set(agents.map(a => a.category).filter(Boolean))).sort(),
    [agents]
  )
  const applications = useMemo(
    () => Array.from(new Set(agents.map(a => a.application).filter(Boolean))).sort(),
    [agents]
  )

  // Cadence helper at the top-level — used in both filter + sort.
  const cadenceOf = (a: AgentSummary): Cadence => {
    if (!a.enabled || !a.cron_expr) return 'disabled'
    return classifyCron(a.cron_expr)
  }

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = agents.filter(a => {
      if (q) {
        const hay = `${a.id} ${a.name || ''} ${a.category || ''} ${a.application || ''} ${a.cron_expr || ''}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      if (categoryFilter !== 'all' && a.category !== categoryFilter) return false
      if (appFilter !== 'all' && a.application !== appFilter) return false
      if (cadenceFilter !== 'all' && cadenceOf(a) !== cadenceFilter) return false
      if (enabledFilter === 'enabled' && !a.enabled) return false
      if (enabledFilter === 'disabled' && a.enabled) return false
      return true
    })

    const dir = sortDir === 'asc' ? 1 : -1
    return filtered.sort((a, b) => {
      let av = '', bv = ''
      switch (sortKey) {
        case 'name':        av = (a.name || ''); bv = (b.name || ''); break
        case 'category':    av = (a.category || ''); bv = (b.category || ''); break
        case 'application': av = (a.application || ''); bv = (b.application || ''); break
        case 'cron':        av = (a.cron_expr || ''); bv = (b.cron_expr || ''); break
        case 'enabled':     av = a.enabled ? '0' : '1'; bv = b.enabled ? '0' : '1'; break
        case 'cadence':     av = cadenceOf(a); bv = cadenceOf(b); break
        case 'id':
        default:            av = a.id; bv = b.id; break
      }
      const cmp = av.localeCompare(bv)
      return cmp !== 0 ? cmp * dir : a.id.localeCompare(b.id)
    })
  }, [agents, search, categoryFilter, appFilter, cadenceFilter, enabledFilter, sortKey, sortDir])

  const filtersActive = (
    !!search.trim() ||
    categoryFilter !== 'all' ||
    appFilter !== 'all' ||
    cadenceFilter !== 'all' ||
    enabledFilter !== 'all' ||
    sortKey !== null
  )
  const clearFilters = () => {
    setSearch(''); setCategoryFilter('all'); setAppFilter('all')
    setCadenceFilter('all'); setEnabledFilter('all')
    setSortKey(null); setSortDir('asc')
  }

  // Snapshot of current cadence per agent (live or pending override).
  function currentCadenceOf(a: AgentSummary): Cadence {
    if (a.id in pending) return pending[a.id]
    if (!a.enabled || !a.cron_expr) return 'disabled'
    return classifyCron(a.cron_expr)
  }

  async function applyCadence(a: AgentSummary, target: Cadence, minuteOffset?: number) {
    if (target === 'custom') return  // no-op
    const m = minuteOffset ?? existingMinuteOffset(a.cron_expr)
    const newCron = buildCron(target, m)
    setSaving(s => ({ ...s, [a.id]: true }))
    try {
      if (newCron === null) {
        // Custom — don't touch
      } else if (newCron === '') {
        // Disable
        await api.disableAgent(a.id)
      } else {
        // If the agent was previously disabled, re-enable as part of setting a schedule
        const patchBody: Partial<AgentSummary> = { cron_expr: newCron }
        if (!a.enabled) patchBody.enabled = true
        await api.patchAgent(a.id, patchBody)
      }
      // Re-fetch this agent's row
      await loadAgents()
      setPending(p => { const { [a.id]: _, ...rest } = p; return rest })
    } catch (e) {
      setError(`${a.id}: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(s => { const { [a.id]: _, ...rest } = s; return rest })
    }
  }

  async function bulkApplyFiveHours() {
    if (!confirm(
      'Apply "every 5 hours" to all enabled, currently-scheduled agents?\n\n' +
      'Minute offsets are spaced 2 minutes apart so timers do not all fire at once. ' +
      'Disabled agents are not touched.'
    )) return
    setBulkBusy(true)
    setError(null)
    try {
      const targets = agents
        .filter(a => a.enabled && (a.cron_expr || '').trim())
        .sort((x, y) => x.id.localeCompare(y.id))
      let i = 0
      for (const a of targets) {
        const minute = (i * 2) % 60
        const cron = buildCron('5h', minute)!
        try { await api.patchAgent(a.id, { cron_expr: cron }) }
        catch (e) { console.error('bulk patch failed for', a.id, e) }
        i++
      }
      setFlash(`Applied 5h cadence to ${targets.length} agents.`)
      await loadAgents()
    } finally {
      setBulkBusy(false)
      setTimeout(() => setFlash(null), 6000)
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-ink-900 tracking-tight">Settings · Schedules</h1>
          <p className="text-sm text-ink-600 mt-1">
            Pick a cadence for each agent. Changes write to the registry and refresh systemd timers
            on the host. After bulk-applying, run <code className="text-xs bg-surface-subtle px-1 rounded">register-agents.sh</code>{' '}
            once to also persist to <code className="text-xs bg-surface-subtle px-1 rounded">manifest.json</code>{' '}
            on disk (otherwise re-registering will revert).
          </p>
        </div>
        <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
          <input
            type="search"
            placeholder="Search id, name, category, app, cron…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="px-3 py-2 bg-surface-card border border-surface-divider rounded-md text-sm w-full sm:w-72 focus:outline-none focus:border-accent-500 focus:ring-2 focus:ring-accent-500/20"
          />
          <button
            disabled={bulkBusy}
            onClick={bulkApplyFiveHours}
            className="btn-primary whitespace-nowrap disabled:opacity-50"
            title="Set every enabled+scheduled agent to a 5-hour cadence with staggered minute offsets"
          >
            {bulkBusy ? 'Applying…' : 'Set all to 5h'}
          </button>
        </div>
      </header>

      {/* Filters: pin to a single value across each dimension. Clicking a
          column header below sorts the table by that column (asc/desc). */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <select
          value={categoryFilter}
          onChange={e => setCategoryFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-xs"
        >
          <option value="all">all categories</option>
          {categories.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select
          value={appFilter}
          onChange={e => setAppFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-xs"
        >
          <option value="all">all applications</option>
          {applications.map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        <select
          value={cadenceFilter}
          onChange={e => setCadenceFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-xs"
        >
          <option value="all">all cadences</option>
          {CADENCE_ORDER.map(c => <option key={c} value={c}>{CADENCE_LABELS[c]}</option>)}
        </select>
        <select
          value={enabledFilter}
          onChange={e => setEnabledFilter(e.target.value as 'all' | 'enabled' | 'disabled')}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-xs"
        >
          <option value="all">all states</option>
          <option value="enabled">enabled only</option>
          <option value="disabled">disabled only</option>
        </select>
        {filtersActive && (
          <button
            onClick={clearFilters}
            className="px-2 py-1.5 text-ink-500 hover:text-ink-900 text-xs"
          >clear all</button>
        )}
        <span className="text-ink-400 ml-auto">
          {visible.length} of {agents.length} agents
        </span>
      </div>

      {flash && (
        <div className="bg-emerald-50 border border-emerald-200 text-emerald-800 px-3 py-2 rounded-md text-sm">
          {flash}
        </div>
      )}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-800 px-3 py-2 rounded-md text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-ink-500 text-sm">Loading agents…</div>
      ) : (
        <div className="bg-surface-card border border-surface-divider rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-surface-subtle text-ink-600 text-xs uppercase tracking-wide select-none">
              <tr>
                <SortableTh keyName="id"       label="Agent"        sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <SortableTh keyName="category" label="Category"     sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} extraCls="hidden md:table-cell" />
                <SortableTh keyName="cron"     label="Current cron" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <SortableTh keyName="cadence"  label="Cadence"      sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <th className="px-3 py-2 font-medium w-20"></th>
              </tr>
            </thead>
            <tbody>
              {visible.map(a => {
                const cur = currentCadenceOf(a)
                const isPending = a.id in pending
                const busy = !!saving[a.id]
                return (
                  <tr key={a.id} className="border-t border-surface-divider hover:bg-surface-subtle/50">
                    <td className="px-3 py-2 align-top">
                      <a
                        href={`/agents/${encodeURIComponent(a.id)}`}
                        className="font-mono text-xs text-accent-700 hover:underline"
                      >{a.id}</a>
                      <div className="text-ink-700 text-xs mt-0.5">{a.name}</div>
                    </td>
                    <td className="px-3 py-2 align-top hidden md:table-cell">
                      <span className="text-ink-600 text-xs">{a.category || '—'}</span>
                    </td>
                    <td className="px-3 py-2 align-top">
                      <code className="text-xs text-ink-700 bg-surface-subtle px-1.5 py-0.5 rounded">
                        {a.cron_expr || '(no schedule)'}
                      </code>
                      {!a.enabled && (
                        <span className="ml-2 text-[10px] text-amber-700 bg-amber-50 border border-amber-200 px-1 rounded">disabled</span>
                      )}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <select
                        value={cur}
                        disabled={busy}
                        onChange={e => {
                          const next = e.target.value as Cadence
                          setPending(p => ({ ...p, [a.id]: next }))
                        }}
                        className="px-2 py-1 bg-surface-card border border-surface-divider rounded-md text-xs focus:outline-none focus:border-accent-500"
                      >
                        {CADENCE_ORDER.map(c => (
                          <option key={c} value={c}>{CADENCE_LABELS[c]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-3 py-2 align-top text-right">
                      {isPending && pending[a.id] !== 'custom' && (
                        <button
                          disabled={busy}
                          onClick={() => applyCadence(a, pending[a.id])}
                          className="btn-primary text-xs px-2 py-1 disabled:opacity-50"
                        >{busy ? '…' : 'Save'}</button>
                      )}
                    </td>
                  </tr>
                )
              })}
              {visible.length === 0 && (
                <tr><td colSpan={5} className="px-3 py-6 text-center text-ink-500 text-sm">No agents match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <details className="bg-surface-card border border-surface-divider rounded-lg p-3 text-xs text-ink-600">
        <summary className="cursor-pointer font-medium text-ink-800">How cadences map to cron</summary>
        <ul className="mt-2 ml-5 list-disc space-y-1 font-mono">
          <li>Every hour → <code>{'{minute}'} * * * *</code></li>
          <li>Every 2 hours → <code>{'{minute}'} */2 * * *</code></li>
          <li>Every 5 hours → <code>{'{minute}'} */5 * * *</code></li>
          <li>Every 12 hours → <code>{'{minute}'} */12 * * *</code></li>
          <li>Daily → <code>{'{minute}'} 6 * * *</code> (06:00 local)</li>
          <li>Custom → don't change the cron — preserves anything that doesn't match a preset</li>
        </ul>
        <p className="mt-2">Minute offsets stagger across 0–58 in 2-minute steps so timers don't all fire at once. The bulk action keeps the same staggering when applying 5h to everything at once.</p>
      </details>
    </div>
  )
}

// Sortable column header — click to sort by that column. Toggles asc/desc
// when the same column is clicked again.
function SortableTh({
  keyName, label, sortKey, sortDir, onClick, extraCls = '',
}: {
  keyName: SortKey
  label: string
  sortKey: SortKey | null
  sortDir: 'asc' | 'desc'
  onClick: (k: SortKey) => void
  extraCls?: string
}) {
  const active = sortKey === keyName
  const arrow = active ? (sortDir === 'asc' ? '↑' : '↓') : ''
  return (
    <th
      onClick={() => onClick(keyName)}
      className={`text-left px-3 py-2 font-medium cursor-pointer hover:text-ink-900 ${extraCls}`}
      title={`Sort by ${label.toLowerCase()}`}
    >
      <span>{label}</span>
      {arrow && <span className="ml-1 text-accent-700">{arrow}</span>}
    </th>
  )
}
