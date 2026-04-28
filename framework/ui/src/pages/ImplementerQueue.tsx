// Implementer Queue — shows pending dispatch items, live LLM output from the
// implementer, and recent dispatch history so the user can see what's been
// queued from their email replies and what's currently running.

import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { DispatchEntry } from '../api/types'

type PendingItem = {
  agent_id: string
  request_id?: string
  site?: string
  from_run?: string
  rec_ids?: string[]
  action?: string
  ts?: string
  _key?: string
}

function fmtTs(ts?: string): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleString()
}

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function fmtAgo(ts?: string): string {
  if (!ts) return ''
  const sec = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000))
  if (sec < 60) return `${sec}s ago`
  const min = Math.round(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.round(min / 60)
  if (hr < 24) return `${hr}h ago`
  return `${Math.round(hr / 24)}d ago`
}

// ── Live LLM panel ──────────────────────────────────────────────────────────

function LivePanel() {
  const [content, setContent] = useState('')
  const [isActive, setIsActive] = useState(false)
  const [runTs, setRunTs] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')
  const [error, setError] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    let alive = true
    const fetch = async () => {
      try {
        const r = await api.getLiveLLMOutput('seo-implementer')
        if (!alive) return
        setContent(r.content || '')
        setIsActive(r.is_active ?? false)
        setRunTs(r.run_ts || '')
        setUpdatedAt(r.updated_at || '')
        setError('')
      } catch (e: any) {
        if (!alive) return
        setError(e?.message || String(e))
      }
    }
    void fetch()
    const ms = isActive ? 2000 : 8000
    const id = setInterval(fetch, ms)
    return () => { alive = false; clearInterval(id) }
  }, [isActive])

  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [content, autoScroll])

  return (
    <div className="card-surface p-4 space-y-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-sm font-semibold text-ink-900 flex items-center gap-2">
          <span>🧠</span> Live LLM output — seo-implementer
          {isActive ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-status-running-bg text-status-running-fg border border-status-running-glow/40">
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
              tailing
            </span>
          ) : (
            <span className="text-ink-400 text-xs font-normal">
              {updatedAt ? `updated ${fmtAgo(updatedAt)}` : 'idle'}
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          {runTs && (
            <span className="text-xs text-ink-400 font-mono">{runTs}</span>
          )}
          <label className="flex items-center gap-1.5 text-xs text-ink-500 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={e => setAutoScroll(e.target.checked)}
              className="accent-accent-500 w-3.5 h-3.5"
            />
            auto-scroll
          </label>
        </div>
      </div>
      {error && (
        <div className="text-xs text-status-failure-fg bg-status-failure-bg rounded px-2 py-1">{error}</div>
      )}
      <div
        className="bg-ink-950 rounded-lg p-3 font-mono text-xs text-green-300 whitespace-pre-wrap overflow-auto max-h-[420px] border border-surface-divider"
        onScroll={() => setAutoScroll(false)}
      >
        {content || <span className="text-ink-500">No output yet — waiting for the implementer to run…</span>}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

// ── Dispatch detail modal ────────────────────────────────────────────────────

function DispatchDetail({ dispatch, onClose }: { dispatch: DispatchEntry; onClose: () => void }) {
  const [log, setLog] = useState<(DispatchEntry & { content: string }) | null>(null)
  const [loading, setLoading] = useState(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setLoading(true)
    api.getDispatchLog(dispatch.id).then(r => {
      setLog(r)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [dispatch.id])

  useEffect(() => {
    if (!loading && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [loading, log?.content])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-surface-card rounded-xl shadow-xl w-full max-w-4xl mx-4 overflow-hidden flex flex-col"
        style={{ maxHeight: '85vh' }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b border-surface-divider bg-surface-subtle">
          <div>
            <span className="font-semibold text-ink-900 text-sm">
              {dispatch.site} — {dispatch.run_ts}
            </span>
            <span className={`ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
              dispatch.status === 'running'
                ? 'bg-status-running-bg text-status-running-fg border border-status-running-glow/40'
                : 'bg-status-success-bg text-status-success-fg border border-status-success-glow/40'
            }`}>
              {dispatch.status === 'running' ? '⚙ running' : '✓ done'}
            </span>
          </div>
          <button onClick={onClose} className="text-ink-400 hover:text-ink-700 text-lg leading-none px-2">✕</button>
        </div>
        <div className="px-5 py-3 border-b border-surface-divider bg-surface-subtle/50 flex items-center gap-4 text-xs text-ink-500 flex-wrap">
          <span><b className="text-ink-700">{dispatch.rec_count}</b> recs: {dispatch.rec_ids.slice(0, 8).join(', ')}{dispatch.rec_ids.length > 8 ? ` + ${dispatch.rec_ids.length - 8} more` : ''}</span>
          {dispatch.commit_sha && <span>commit <code className="text-accent-600">{dispatch.commit_sha.slice(0, 8)}</code></span>}
          <span>{fmtAgo(dispatch.started_at)}</span>
          {log && <span>{fmtSize(log.size_bytes)}</span>}
        </div>
        <div className="flex-1 overflow-auto p-4">
          {loading ? (
            <div className="text-ink-400 text-sm text-center py-8">Loading log…</div>
          ) : (
            <pre className="bg-ink-950 rounded-lg p-3 font-mono text-xs text-green-300 whitespace-pre-wrap overflow-auto">
              {log?.content || '(empty log)'}
              <div ref={bottomRef} />
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Recent dispatches table ──────────────────────────────────────────────────

function RecentDispatches({ dispatches }: { dispatches: DispatchEntry[] }) {
  const [selected, setSelected] = useState<DispatchEntry | null>(null)

  if (!dispatches.length) {
    return (
      <div className="card-surface p-8 text-center text-ink-400 text-sm">
        No recent dispatch logs found in <code className="text-xs">/tmp/reusable-agents-logs/</code>
      </div>
    )
  }

  return (
    <>
      <div className="card-surface overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-surface-subtle border-b border-surface-divider text-xs text-ink-500 uppercase tracking-wide">
              <th className="px-4 py-2.5 text-left font-medium">Site</th>
              <th className="px-4 py-2.5 text-left font-medium">Run TS</th>
              <th className="px-4 py-2.5 text-left font-medium">Recs</th>
              <th className="px-4 py-2.5 text-left font-medium">Status</th>
              <th className="px-4 py-2.5 text-left font-medium">Commit</th>
              <th className="px-4 py-2.5 text-left font-medium">Started</th>
              <th className="px-4 py-2.5 text-left font-medium">Size</th>
              <th className="px-4 py-2.5 text-left font-medium"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-divider">
            {dispatches.map(d => (
              <tr
                key={d.id}
                className="hover:bg-surface-subtle/60 cursor-pointer transition-colors"
                onClick={() => setSelected(d)}
              >
                <td className="px-4 py-2.5 font-medium text-ink-900 capitalize">{d.site}</td>
                <td className="px-4 py-2.5 font-mono text-xs text-ink-600">{d.run_ts}</td>
                <td className="px-4 py-2.5 text-ink-700">
                  <span className="font-semibold">{d.rec_count}</span>
                  {d.rec_ids.length > 0 && (
                    <span className="ml-1 text-xs text-ink-400">
                      {d.rec_ids.slice(0, 3).join(', ')}{d.rec_ids.length > 3 ? '…' : ''}
                    </span>
                  )}
                </td>
                <td className="px-4 py-2.5">
                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                    d.status === 'running'
                      ? 'bg-status-running-bg text-status-running-fg border border-status-running-glow/40'
                      : 'bg-status-success-bg text-status-success-fg border border-status-success-glow/40'
                  }`}>
                    {d.status === 'running' ? <><span className="animate-pulse">⚙</span> running</> : '✓ done'}
                  </span>
                </td>
                <td className="px-4 py-2.5 font-mono text-xs text-accent-600">
                  {d.commit_sha ? d.commit_sha.slice(0, 8) : '—'}
                </td>
                <td className="px-4 py-2.5 text-ink-500 text-xs whitespace-nowrap">
                  {fmtAgo(d.started_at)}
                </td>
                <td className="px-4 py-2.5 text-ink-400 text-xs">{fmtSize(d.size_bytes)}</td>
                <td className="px-4 py-2.5 text-right">
                  <button
                    onClick={e => { e.stopPropagation(); setSelected(d) }}
                    className="text-xs text-accent-600 hover:text-accent-700 font-medium"
                  >
                    view log →
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selected && <DispatchDetail dispatch={selected} onClose={() => setSelected(null)} />}
    </>
  )
}

// ── Pending queue ────────────────────────────────────────────────────────────

function PendingQueue({ pending }: { pending: PendingItem[] }) {
  if (!pending.length) {
    return (
      <div className="card-surface p-6 text-center text-ink-400 text-sm">
        Responses queue is empty — replies are dispatched immediately on arrival.
      </div>
    )
  }
  return (
    <div className="card-surface divide-y divide-surface-divider">
      {pending.map((item, i) => (
        <div key={item._key ?? i} className="px-4 py-3 flex items-start gap-3">
          <div className="w-2 h-2 rounded-full bg-amber-400 mt-1.5 shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-medium text-ink-900 capitalize">{item.site || item.agent_id}</span>
              <span className="text-xs text-ink-400 font-mono">{item.from_run}</span>
              <span className="text-xs px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200">
                {item.action || 'pending'}
              </span>
            </div>
            {item.rec_ids && item.rec_ids.length > 0 && (
              <div className="text-xs text-ink-500 mt-0.5">
                {item.rec_ids.length} recs: {item.rec_ids.join(', ')}
              </div>
            )}
            <div className="text-xs text-ink-400 mt-0.5">{fmtTs(item.ts)}</div>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Main page ────────────────────────────────────────────────────────────────

export default function ImplementerQueue() {
  const [pending, setPending] = useState<PendingItem[]>([])
  const [dispatches, setDispatches] = useState<DispatchEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)

  const refresh = async () => {
    try {
      const data = await api.implementerQueue(30)
      setPending(data.pending)
      setDispatches(data.dispatches)
      setError('')
    } catch (e: any) {
      setError(e?.message || String(e))
    } finally {
      setLoading(false)
      setLastRefresh(new Date())
    }
  }

  useEffect(() => {
    void refresh()
    const id = setInterval(refresh, 15000)
    return () => clearInterval(id)
  }, [])

  const running = dispatches.filter(d => d.status === 'running')
  const done = dispatches.filter(d => d.status !== 'running')

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-ink-900">Implementer Queue</h1>
          <p className="text-sm text-ink-500 mt-0.5">
            Email replies dispatched to the SEO implementer — pending queue, live output, and recent dispatch history.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {lastRefresh && (
            <span className="text-xs text-ink-400">
              refreshed {fmtAgo(lastRefresh.toISOString())}
            </span>
          )}
          <button
            onClick={() => { setLoading(true); void refresh() }}
            className="btn-secondary text-xs"
            disabled={loading}
          >
            {loading ? 'Refreshing…' : '↺ Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-status-failure-bg border border-status-failure-glow/40 rounded-lg px-4 py-3 text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'Pending', value: pending.length, color: pending.length > 0 ? 'text-amber-600' : 'text-ink-400' },
          { label: 'Running', value: running.length, color: running.length > 0 ? 'text-status-running-fg' : 'text-ink-400' },
          { label: 'Recent dispatches', value: done.length, color: 'text-ink-700' },
        ].map(stat => (
          <div key={stat.label} className="card-surface p-4 text-center">
            <div className={`text-3xl font-bold ${stat.color}`}>{stat.value}</div>
            <div className="text-xs text-ink-500 mt-1">{stat.label}</div>
          </div>
        ))}
      </div>

      {/* Live LLM output */}
      <LivePanel />

      {/* Pending queue */}
      {pending.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-ink-700 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            Pending queue ({pending.length})
          </h2>
          <PendingQueue pending={pending} />
        </section>
      )}

      {/* Running dispatches */}
      {running.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-ink-700 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-status-running-glow animate-pulse" />
            Currently running ({running.length})
          </h2>
          <RecentDispatches dispatches={running} />
        </section>
      )}

      {/* Recent dispatches */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-ink-700">
          Recent dispatches ({done.length})
        </h2>
        <RecentDispatches dispatches={done} />
      </section>
    </div>
  )
}
