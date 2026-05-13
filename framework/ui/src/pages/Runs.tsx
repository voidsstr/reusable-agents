// Runs — cross-agent run history. Newest first by default; sortable
// columns; filters for agent / status / application / category / search /
// since-date. Rows expand to fetch + show full run detail (progress,
// decisions, recommendations, responses, deploy) on demand.

import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { AgentSummary, RunDetail, RunSummary } from '../api/types'

type Row = RunSummary & { agent_name?: string; application?: string; category?: string }
type SortKey = 'started_at' | 'agent_id' | 'status' | 'progress' | 'iteration_count'
type SortDir = 'asc' | 'desc'

const STATUS_STYLES: Record<string, string> = {
  success:   'bg-status-success-bg text-status-success-fg border-status-success-glow',
  failure:   'bg-status-failure-bg text-status-failure-fg border-status-failure-glow',
  running:   'bg-status-running-bg text-status-running-fg border-status-running-glow',
  starting:  'bg-status-starting-bg text-status-starting-fg border-status-starting-glow',
  blocked:   'bg-status-blocked-bg text-status-blocked-fg border-status-blocked-glow',
  cancelled: 'bg-surface-subtle text-ink-600 border-surface-divider',
  idle:      'bg-surface-subtle text-ink-500 border-surface-divider',
}

const PAGE_SIZE = 100

export default function Runs() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [agents, setAgents] = useState<AgentSummary[]>([])

  // Filters (persisted)
  const [agentFilter, setAgentFilter] = useState<string>(() => localStorage.getItem('runs-agent') || '')
  const [statusFilter, setStatusFilter] = useState<string>(() => localStorage.getItem('runs-status') || '')
  const [appFilter, setAppFilter] = useState<string>(() => localStorage.getItem('runs-app') || '')
  const [catFilter, setCatFilter] = useState<string>(() => localStorage.getItem('runs-cat') || '')
  const [search, setSearch] = useState('')
  const [since, setSince] = useState('')
  const [offset, setOffset] = useState(0)

  useEffect(() => { localStorage.setItem('runs-agent', agentFilter) }, [agentFilter])
  useEffect(() => { localStorage.setItem('runs-status', statusFilter) }, [statusFilter])
  useEffect(() => { localStorage.setItem('runs-app', appFilter) }, [appFilter])
  useEffect(() => { localStorage.setItem('runs-cat', catFilter) }, [catFilter])

  // Sort (client-side over the current page; server sorts by started_at desc)
  const [sortKey, setSortKey] = useState<SortKey>('started_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  // Reset paging when filters change
  useEffect(() => { setOffset(0) }, [agentFilter, statusFilter, appFilter, catFilter, search, since])

  const refresh = async () => {
    setLoading(true)
    try {
      const r = await api.listAllRuns({
        limit: PAGE_SIZE,
        offset,
        agent_id: agentFilter || undefined,
        status: statusFilter || undefined,
        application: appFilter || undefined,
        category: catFilter || undefined,
        since: since ? new Date(since).toISOString() : undefined,
        q: search || undefined,
      })
      setRows(r.runs)
      setTotal(r.total)
      setError('')
    } catch (e) {
      setError(String((e as Error).message || e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset, agentFilter, statusFilter, appFilter, catFilter, since])

  // Debounce free-text search
  useEffect(() => {
    const id = setTimeout(refresh, 300)
    return () => clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  // Agent list (for the agent filter dropdown + app/category options)
  useEffect(() => {
    api.listAgents().then(setAgents).catch(() => { /* ignore */ })
  }, [])

  const applications = useMemo(() => Array.from(new Set(agents.map(a => a.application || 'shared'))).sort(), [agents])
  const categories   = useMemo(() => Array.from(new Set(agents.map(a => a.category))).sort(), [agents])
  const statuses     = ['success', 'failure', 'running', 'starting', 'blocked', 'cancelled', 'idle']

  const sorted = useMemo(() => {
    const copy = [...rows]
    const cmp = (a: Row, b: Row): number => {
      const av: string | number = (a[sortKey] ?? '') as string | number
      const bv: string | number = (b[sortKey] ?? '') as string | number
      if (av === bv) return 0
      return av < bv ? -1 : 1
    }
    copy.sort((a, b) => sortDir === 'asc' ? cmp(a, b) : -cmp(a, b))
    return copy
  }, [rows, sortKey, sortDir])

  const toggleSort = (k: SortKey) => {
    if (sortKey === k) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(k); setSortDir(k === 'started_at' ? 'desc' : 'asc') }
  }

  const sortIcon = (k: SortKey) => sortKey === k ? (sortDir === 'asc' ? '▲' : '▼') : '·'

  const clearFilters = () => {
    setAgentFilter(''); setStatusFilter(''); setAppFilter(''); setCatFilter('')
    setSearch(''); setSince('')
  }
  const anyFilter = agentFilter || statusFilter || appFilter || catFilter || search || since

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-ink-900 tracking-tight">Runs</h1>
          <div className="text-xs sm:text-sm text-ink-500 mt-0.5">
            {loading ? 'Loading…' : (
              <>
                <span className="font-medium text-ink-700">{total}</span> runs
                {anyFilter ? ' matching filters' : ' across all agents'}
                {' · '}newest first
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {anyFilter && (
            <button onClick={clearFilters} className="btn-secondary text-xs">Clear filters</button>
          )}
          <button onClick={refresh} className="btn-secondary" title="Refresh" aria-label="Refresh">↻</button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2.5 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {/* Filters */}
      <div className="bg-surface-card border border-surface-divider rounded-xl p-4 shadow-card grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-3">
        <FilterSelect label="Agent" value={agentFilter} onChange={setAgentFilter}
          options={[{ key: '', label: 'all agents' }, ...agents.map(a => ({ key: a.id, label: a.name || a.id }))]} />
        <FilterSelect label="Status" value={statusFilter} onChange={setStatusFilter}
          options={[{ key: '', label: 'any status' }, ...statuses.map(s => ({ key: s, label: s }))]} />
        <FilterSelect label="Application" value={appFilter} onChange={setAppFilter}
          options={[{ key: '', label: 'any app' }, ...applications.map(a => ({ key: a, label: a }))]} />
        <FilterSelect label="Category" value={catFilter} onChange={setCatFilter}
          options={[{ key: '', label: 'any category' }, ...categories.map(c => ({ key: c, label: c }))]} />
        <div>
          <div className="text-[10px] uppercase text-ink-400 font-semibold tracking-wider mb-1">Since</div>
          <input type="datetime-local" value={since} onChange={e => setSince(e.target.value)}
            className="w-full px-2.5 py-1.5 bg-surface-card border border-surface-divider rounded-md text-sm focus:outline-none focus:border-accent-500" />
        </div>
        <div>
          <div className="text-[10px] uppercase text-ink-400 font-semibold tracking-wider mb-1">Search</div>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="summary / agent…"
            className="w-full px-2.5 py-1.5 bg-surface-card border border-surface-divider rounded-md text-sm focus:outline-none focus:border-accent-500" />
        </div>
      </div>

      {/* Table */}
      <div className="bg-surface-card border border-surface-divider rounded-xl shadow-card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-surface-subtle text-ink-600 text-xs uppercase tracking-wider">
              <tr>
                <th className="w-6 px-2 py-2"></th>
                <Th onClick={() => toggleSort('started_at')} icon={sortIcon('started_at')}>Started</Th>
                <Th onClick={() => toggleSort('agent_id')}   icon={sortIcon('agent_id')}>Agent</Th>
                <Th onClick={() => toggleSort('status')}     icon={sortIcon('status')}>Status</Th>
                <Th onClick={() => toggleSort('progress')}   icon={sortIcon('progress')}>Progress</Th>
                <Th onClick={() => toggleSort('iteration_count')} icon={sortIcon('iteration_count')}>Iter</Th>
                <th className="px-3 py-2 text-left font-semibold">Summary</th>
                <th className="px-3 py-2 text-left font-semibold">Run TS</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={8} className="text-center py-12 text-ink-400">Loading…</td></tr>
              ) : sorted.length === 0 ? (
                <tr><td colSpan={8} className="text-center py-12 text-ink-400">No runs.</td></tr>
              ) : sorted.map(r => (
                <RunRow key={`${r.agent_id}::${r.run_ts}`} row={r} />
              ))}
            </tbody>
          </table>
        </div>

        {/* Pager */}
        <div className="flex items-center justify-between gap-3 px-3 py-2 border-t border-surface-divider text-xs text-ink-500">
          <span>
            {total === 0 ? '—' : (
              <>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</>
            )}
          </span>
          <div className="flex gap-2">
            <button onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}
              className="btn-secondary text-xs disabled:opacity-40 disabled:cursor-not-allowed">← Prev</button>
            <button onClick={() => setOffset(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total}
              className="btn-secondary text-xs disabled:opacity-40 disabled:cursor-not-allowed">Next →</button>
          </div>
        </div>
      </div>
    </div>
  )
}

function Th({ children, onClick, icon }: { children: React.ReactNode; onClick: () => void; icon: string }) {
  return (
    <th
      onClick={onClick}
      className="px-3 py-2 text-left font-semibold cursor-pointer select-none hover:bg-surface-divider/50"
    >
      {children} <span className="opacity-50 ml-1">{icon}</span>
    </th>
  )
}

function FilterSelect({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: { key: string; label: string }[]
}) {
  return (
    <div>
      <div className="text-[10px] uppercase text-ink-400 font-semibold tracking-wider mb-1">{label}</div>
      <select value={value} onChange={e => onChange(e.target.value)}
        className="w-full px-2.5 py-1.5 bg-surface-card border border-surface-divider rounded-md text-sm focus:outline-none focus:border-accent-500">
        {options.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
      </select>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLES[status] || 'bg-surface-subtle text-ink-500 border-surface-divider'
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] font-medium whitespace-nowrap ${cls}`}>
      {status || 'unknown'}
    </span>
  )
}

function fmtTs(s: string | null | undefined) {
  if (!s) return '—'
  try {
    const d = new Date(s)
    if (isNaN(d.getTime())) return s
    return d.toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  } catch { return s }
}

function RunRow({ row }: { row: Row }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !detail && !loading) {
      setLoading(true)
      try {
        const d = await api.getRun(row.agent_id, row.run_ts)
        setDetail(d)
      } catch (e) {
        setErr(String((e as Error).message || e))
      } finally { setLoading(false) }
    }
  }

  return (
    <>
      <tr className="border-t border-surface-divider hover:bg-surface-subtle/40 cursor-pointer" onClick={toggle}>
        <td className="px-2 py-2 text-ink-400 text-center w-6">{open ? '▾' : '▸'}</td>
        <td className="px-3 py-2 whitespace-nowrap text-ink-700">{fmtTs(row.started_at)}</td>
        <td className="px-3 py-2 whitespace-nowrap">
          <Link to={`/agents/${encodeURIComponent(row.agent_id)}`} onClick={(e) => e.stopPropagation()}
            className="text-accent-700 hover:underline">{row.agent_name || row.agent_id}</Link>
          <div className="text-[10px] text-ink-400">{row.application} · {row.category}</div>
        </td>
        <td className="px-3 py-2"><StatusBadge status={row.status} /></td>
        <td className="px-3 py-2 text-ink-700">{Math.round((row.progress || 0) * 100)}%</td>
        <td className="px-3 py-2 text-ink-700">{row.iteration_count}</td>
        <td className="px-3 py-2 text-ink-600 max-w-md truncate" title={row.summary}>{row.summary || '—'}</td>
        <td className="px-3 py-2 font-mono text-[11px] text-ink-500">{row.run_ts}</td>
      </tr>
      {open && (
        <tr className="border-t border-surface-divider bg-surface-subtle/30">
          <td colSpan={8} className="px-4 py-3">
            {loading && <div className="text-ink-400 text-sm">Loading run detail…</div>}
            {err && <div className="text-status-failure-fg text-sm">{err}</div>}
            {detail && <RunDetailPanel detail={detail} />}
          </td>
        </tr>
      )}
    </>
  )
}

function RunDetailPanel({ detail }: { detail: RunDetail }) {
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-500">
        <span><b>Agent:</b> {detail.agent_id}</span>
        <span><b>Run:</b> {detail.run_ts}</span>
        <Link to={`/agents/${encodeURIComponent(detail.agent_id)}`} className="text-accent-700 hover:underline">open agent →</Link>
      </div>
      {detail.context_summary_md && (
        <Section title="Context summary">
          <pre className="whitespace-pre-wrap font-mono text-xs text-ink-700 bg-surface-card border border-surface-divider rounded p-2 max-h-64 overflow-auto">
            {detail.context_summary_md}
          </pre>
        </Section>
      )}
      <Section title={`Progress`}>
        <Json value={detail.progress} />
      </Section>
      {detail.decisions && detail.decisions.length > 0 && (
        <Section title={`Decisions (${detail.decisions.length})`}>
          <Json value={detail.decisions} />
        </Section>
      )}
      {detail.recommendations !== null && detail.recommendations !== undefined && (
        <Section title="Recommendations"><Json value={detail.recommendations} /></Section>
      )}
      {detail.responses !== null && detail.responses !== undefined && (
        <Section title="Responses"><Json value={detail.responses} /></Section>
      )}
      {detail.deploy !== null && detail.deploy !== undefined && (
        <Section title="Deploy"><Json value={detail.deploy} /></Section>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="border border-surface-divider rounded-md bg-surface-card">
      <button onClick={() => setOpen(o => !o)}
        className="w-full text-left px-3 py-1.5 text-xs font-semibold text-ink-700 uppercase tracking-wider flex justify-between items-center hover:bg-surface-subtle">
        {title}
        <span className="text-ink-400">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="p-2">{children}</div>}
    </div>
  )
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="whitespace-pre-wrap font-mono text-[11px] text-ink-700 bg-surface-page border border-surface-divider rounded p-2 max-h-80 overflow-auto">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}
