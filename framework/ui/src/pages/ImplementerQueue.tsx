// Implementer Queue — shows the batched dispatch chains the responder
// kicked off in response to user email replies. Three sections:
//   • Shipped — chains where every batch is done (or paused/skipped)
//   • In progress — the active chain with a running batch
//   • Queue — pending batches not yet picked up
// Each rec is clickable for full title + summary.md drill-down.
//
// Live LLM panel auto-targets the currently-running batch's source agent
// (instead of a hard-coded id).

import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'

type RecItem = {
  rec_id: string
  title: string
  kind: string
  summary_first_line?: string
  summary_chars?: number
  deferred?: boolean
  applied?: boolean
  implemented?: boolean
  implemented_at?: string
  implemented_via?: string
  shipped?: boolean
  shipped_at?: string
  shipped_tag?: string
  shipped_via?: string
}

type Batch = {
  index: number
  status: string
  rec_count: number
  priority_summary: string
  started_at: string
  completed_at: string
  dispatch_log: string
  rec_items: RecItem[]
}

type Chain = {
  run_dir_basename: string
  dispatch_run_ts: string
  source_run_ts: string
  source_agent: string
  site: string
  batch_size: number
  total_recs: number
  chain_status: string
  mtime_iso: string
  batches: Batch[]
}

function fmtTs(ts?: string): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleString()
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

// Compact relative date: "5m ago", "2h ago", "3d ago", "2w ago".
// Used in the rec list rows where space is tight. Falls back to the
// raw ISO string if parsing fails.
function formatRelDate(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (sec < 60) return `${sec}s ago`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m ago`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr}h ago`
  const day = Math.floor(hr / 24)
  if (day < 14) return `${day}d ago`
  const wk = Math.floor(day / 7)
  return `${wk}w ago`
}

// Absolute, locale-formatted version for tooltips + drill-down rows
// where there's room. Returns a short like "Apr 28 18:09 UTC".
function formatAbsDate(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
    timeZoneName: 'short',
  })
}

function priorityClass(kind: string): string {
  const k = (kind || '').toLowerCase()
  if (k === 'critical' || k === 'must') return 'bg-red-50 text-red-700 ring-red-200'
  if (k === 'high')                       return 'bg-orange-50 text-orange-700 ring-orange-200'
  if (k === 'medium' || k === 'should')   return 'bg-amber-50 text-amber-700 ring-amber-200'
  if (k === 'low' || k === 'could')       return 'bg-blue-50 text-blue-700 ring-blue-200'
  return 'bg-ink-100 text-ink-600 ring-ink-200'
}

function batchStatusClass(s: string): { dot: string; label: string; chip: string } {
  switch (s) {
    case 'running':   return { dot: 'bg-blue-500 animate-pulse', label: 'Running', chip: 'bg-blue-50 text-blue-700 ring-blue-200' }
    case 'completed': return { dot: 'bg-emerald-500', label: 'Done', chip: 'bg-emerald-50 text-emerald-700 ring-emerald-200' }
    case 'paused':    return { dot: 'bg-amber-500', label: 'Paused', chip: 'bg-amber-50 text-amber-700 ring-amber-200' }
    case 'pending':   return { dot: 'bg-ink-300', label: 'Queued', chip: 'bg-ink-100 text-ink-600 ring-ink-200' }
    default:          return { dot: 'bg-ink-300', label: s || '?', chip: 'bg-ink-100 text-ink-600 ring-ink-200' }
  }
}

// ──────────────────────────────────────────────────────────────────────────
// Live LLM panel — auto-targets the running chain's source agent.

function LivePanel({ targetAgent }: { targetAgent: string }) {
  const [content, setContent] = useState('')
  const [isActive, setIsActive] = useState(false)
  const [runTs, setRunTs] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')
  const [error, setError] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const [paused, setPaused] = useState(false)

  useEffect(() => {
    if (!targetAgent) return
    let alive = true
    const fetchOnce = async () => {
      if (paused) return
      try {
        const r = await api.getLiveLLMOutput(targetAgent)
        if (!alive) return
        setContent(r.content || '')
        setIsActive(!!r.is_active)
        setRunTs(r.run_ts || '')
        setUpdatedAt(new Date().toLocaleTimeString())
        setError('')
      } catch (e: unknown) {
        if (alive) setError(String((e as Error).message || e))
      }
    }
    fetchOnce()
    // Poll fast (2s) when active, slow (8s) when idle.
    const id = setInterval(fetchOnce, isActive ? 2000 : 8000)
    return () => { alive = false; clearInterval(id) }
  }, [targetAgent, isActive, paused])

  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'auto', block: 'end' })
    }
  }, [content, autoScroll])

  return (
    <section className="card-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-surface-divider flex items-center justify-between gap-2 flex-wrap bg-surface-subtle/40">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-lg">🧠</span>
          <h2 className="font-semibold text-ink-900 text-[15px] truncate">
            Live LLM — implementer
          </h2>
          <span className={`status-pill text-[10px] ${
            isActive
              ? 'bg-status-running-bg text-status-running-fg ring-status-running-glow/30'
              : 'bg-ink-100 text-ink-500 ring-ink-200'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${
              isActive ? 'bg-status-running-glow animate-pulse' : 'bg-ink-400'
            }`} />
            {isActive ? 'streaming' : 'idle'}
          </span>
        </div>
        <div className="flex items-center gap-3 text-xs text-ink-500 flex-wrap">
          {targetAgent && <span className="font-mono truncate max-w-[180px] sm:max-w-none">{targetAgent}</span>}
          {runTs && <span className="font-mono hidden sm:inline">run: {runTs.slice(0, 28)}</span>}
          {updatedAt && <span>updated {updatedAt}</span>}
          <label className="inline-flex items-center gap-1 cursor-pointer">
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} className="rounded" />
            <span>auto-scroll</span>
          </label>
          <button
            onClick={() => setPaused(p => !p)}
            className="px-2 py-0.5 rounded border border-surface-divider hover:bg-surface-subtle"
            title={paused ? 'Resume polling' : 'Pause polling'}
          >{paused ? '▶ resume' : '⏸ pause'}</button>
        </div>
      </div>
      {error && (
        <div className="px-4 py-2 bg-status-failure-bg text-status-failure-fg text-xs">{error}</div>
      )}
      <div className="bg-ink-950 text-ink-100 max-h-[55vh] sm:max-h-[40vh] overflow-y-auto p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all">
        {content || (
          <span className="text-ink-500 italic">
            {targetAgent
              ? 'No live output yet — implementer is between batches or idle.'
              : 'No active chain to monitor.'}
          </span>
        )}
        <div ref={bottomRef} />
      </div>
    </section>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Rec drill-down modal

function RecDetailModal({
  runDirBasename, recId, onClose,
}: { runDirBasename: string; recId: string; onClose: () => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.getBatchRecDetail>> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    let alive = true
    api.getBatchRecDetail(runDirBasename, recId)
      .then(r => { if (alive) { setData(r); setLoading(false) } })
      .catch(e => { if (alive) { setError(String(e?.message || e)); setLoading(false) } })
    return () => { alive = false }
  }, [runDirBasename, recId])

  // Same click-through guard as VerificationModal — without the 50ms arming
  // delay, the click that opens the modal bubbles to the backdrop and
  // immediately closes it ("flash" symptom on mobile + fast clicks).
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    const t = setTimeout(() => setArmed(true), 50)
    return () => clearTimeout(t)
  }, [])
  const handleBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!armed) return
    if (e.target !== e.currentTarget) return
    onClose()
  }
  // Lock body scroll + close on Esc
  useEffect(() => {
    const orig = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = orig
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-40 bg-ink-900/40 backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4 animate-fade-in"
      onClick={handleBackdropClick}
    >
      <div
        className="bg-surface-card rounded-t-xl sm:rounded-xl shadow-2xl w-full sm:max-w-3xl max-h-[90vh] overflow-y-auto animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-surface-card border-b border-surface-divider px-4 py-3 flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-[10px] uppercase tracking-wide text-ink-500 font-mono">{recId}</div>
            <h3 className="text-base font-semibold text-ink-900 truncate">
              {(data?.rec as { title?: string } | undefined)?.title || (loading ? 'Loading…' : '(no title)')}
            </h3>
          </div>
          <button onClick={onClose} aria-label="Close" className="text-ink-500 hover:text-ink-900 text-xl px-2">×</button>
        </div>

        <div className="p-4 space-y-4">
          {loading && <div className="text-ink-500">Loading…</div>}
          {error && <div className="text-status-failure-fg text-sm">{error}</div>}
          {data && (
            <>
              {/* Rec metadata */}
              <div className="grid grid-cols-2 gap-2 text-xs text-ink-700">
                {Object.entries((data.rec || {}) as Record<string, unknown>)
                  .filter(([k]) => !['title', 'description', 'fix', 'rationale', 'evidence'].includes(k))
                  .slice(0, 8)
                  .map(([k, v]) => (
                    <div key={k}>
                      <span className="text-ink-400">{k}:</span>{' '}
                      <span className="font-mono">{typeof v === 'string' || typeof v === 'number' ? String(v) : JSON.stringify(v)}</span>
                    </div>
                  ))}
              </div>

              {/* Description / rationale / fix */}
              {(data.rec as { description?: string }).description && (
                <Section title="Description">
                  <p className="text-sm text-ink-700">{String((data.rec as { description?: string }).description)}</p>
                </Section>
              )}
              {(data.rec as { rationale?: string }).rationale && (
                <Section title="Rationale">
                  <p className="text-sm text-ink-700">{String((data.rec as { rationale?: string }).rationale)}</p>
                </Section>
              )}
              {(data.rec as { evidence?: string }).evidence && (
                <Section title="Evidence">
                  <p className="text-sm text-ink-700">{String((data.rec as { evidence?: string }).evidence)}</p>
                </Section>
              )}
              {(data.rec as { fix?: string }).fix && (
                <Section title="Proposed fix">
                  <p className="text-sm text-ink-700">{String((data.rec as { fix?: string }).fix)}</p>
                </Section>
              )}

              {/* Implementer's summary.md */}
              {data.summary_md ? (
                <Section title="Implementer summary">
                  <pre className="text-xs bg-surface-subtle p-3 rounded font-mono whitespace-pre-wrap break-words">{data.summary_md}</pre>
                </Section>
              ) : (
                <Section title="Implementer summary">
                  <div className="text-xs text-ink-400 italic">No summary written yet — implementer hasn't processed this rec.</div>
                </Section>
              )}

              {/* Producing-agent's deep context bundle (rec-context) */}
              {data.rec_context && (
                <Section title="Producer context">
                  <div className="space-y-3">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="status-pill bg-accent-50 text-accent-700 ring-accent-500/30">
                        kind: {data.rec_context.kind || '—'}
                      </span>
                      {data.rec_context.summary && (
                        <span className="text-ink-700">{data.rec_context.summary}</span>
                      )}
                    </div>
                    {Object.keys(data.rec_context.fields || {}).length > 0 && (
                      <div>
                        <div className="text-[10px] uppercase text-ink-500 font-semibold mb-1">Fields</div>
                        <pre className="text-xs bg-surface-subtle p-3 rounded font-mono whitespace-pre-wrap break-words">{JSON.stringify(data.rec_context.fields, null, 2)}</pre>
                      </div>
                    )}
                    {data.rec_context.attachments.length > 0 && (
                      <div>
                        <div className="text-[10px] uppercase text-ink-500 font-semibold mb-1">
                          Attachments ({data.rec_context.attachments.length})
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {data.rec_context.attachments.map(name => (
                            <a
                              key={name}
                              href={api.recContextAttachmentUrl(runDirBasename, recId, name)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="status-pill bg-surface-card text-accent-700 ring-surface-divider hover:bg-accent-50 hover:ring-accent-300 text-xs"
                            >
                              📎 {name}
                            </a>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </Section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t border-surface-divider pt-3 first:border-t-0 first:pt-0">
      <h4 className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">{title}</h4>
      {children}
    </section>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Rec row

function RecRow({
  item, onClick,
}: { item: RecItem; onClick: () => void }) {
  // Lifecycle (highest precedence first):
  //   shipped     — code is live in production (deployer pushed)
  //   implemented — code committed locally, but not yet deployed
  //   deferred    — claude wrote DEFERRED/SKIP summary
  //   applied     — non-code DB-write applied (catalog-audit etc.)
  //   untouched   — no summary file yet
  //   wrote       — summary file present but no other state (rare)
  const isShipped = item.shipped
  const isImplemented = item.implemented && !isShipped
  const isDeferred = item.deferred && !isShipped && !isImplemented
  const isApplied = item.applied && !isShipped && !isImplemented
  const isUntouched = !item.summary_chars && !isShipped && !isImplemented && !isApplied && !isDeferred
  const icon = isShipped     ? '🚀'
             : isImplemented ? '✅'
             : isApplied     ? '✅'
             : isDeferred    ? '⏭'
             : isUntouched   ? '⋯'
             :                 '📝'
  return (
    <button
      onClick={onClick}
      className="w-full text-left flex items-center gap-2 px-3 py-2 hover:bg-surface-subtle border-b border-surface-divider last:border-b-0 transition-colors"
    >
      <span className="shrink-0 w-5 text-base text-center" aria-hidden>{icon}</span>
      <span className="font-mono text-[11px] text-ink-500 shrink-0 w-16">{item.rec_id}</span>
      {item.kind && (
        <span className={`status-pill text-[10px] ${priorityClass(item.kind)} shrink-0`}>{item.kind}</span>
      )}
      <span className="flex-1 min-w-0 text-sm text-ink-800 truncate">{item.title || '—'}</span>
      {/* Lifecycle chip — only one shows, in precedence order */}
      {isShipped && (
        <span
          title={`Shipped to production${item.shipped_tag ? ` (tag: ${item.shipped_tag})` : ''}${item.shipped_at ? ` at ${item.shipped_at}` : ''}`}
          className="text-[10px] text-blue-700 bg-blue-50 ring-1 ring-blue-200 px-1.5 py-0.5 rounded shrink-0"
        >🚀 shipped{item.shipped_at ? ` ${formatRelDate(item.shipped_at)}` : ''}</span>
      )}
      {isImplemented && (
        <span
          title={
            item.implemented_via === 'pre-existing'
              ? `Already in code${item.implemented_at ? ` (verified at ${item.implemented_at})` : ''}`
              : `Code committed${item.implemented_at ? ` at ${item.implemented_at}` : ''} — not yet deployed`
          }
          className="text-[10px] text-emerald-700 bg-emerald-50 ring-1 ring-emerald-200 px-1.5 py-0.5 rounded shrink-0"
        >✅ {item.implemented_via === 'pre-existing' ? 'already' : 'implemented'}{item.implemented_at ? ` ${formatRelDate(item.implemented_at)}` : ''}</span>
      )}
      {isApplied && !isImplemented && (
        <span className="text-[10px] text-emerald-700 bg-emerald-50 px-1.5 py-0.5 rounded shrink-0">applied</span>
      )}
      {isDeferred && (
        <span className="text-[10px] text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded shrink-0">deferred</span>
      )}
    </button>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Batch card (collapsible)

function BatchCard({
  batch, runDirBasename, onPickRec, defaultOpen = false,
}: { batch: Batch; runDirBasename: string; onPickRec: (recId: string) => void; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  const s = batchStatusClass(batch.status)
  const appliedN = batch.rec_items.filter(r => r.applied).length
  const deferredN = batch.rec_items.filter(r => r.deferred).length
  const writtenN = batch.rec_items.filter(r => (r.summary_chars || 0) > 0).length

  return (
    <div className="card-surface overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full text-left px-4 py-3 hover:bg-surface-subtle transition-colors flex items-center gap-3"
        aria-expanded={open}
      >
        <span className={`shrink-0 w-2.5 h-2.5 rounded-full ${s.dot}`} aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-ink-900">Batch {batch.index}</span>
            <span className={`status-pill text-[10px] ${s.chip}`}>{s.label}</span>
            <span className="text-xs text-ink-500">{batch.rec_count} recs</span>
            {batch.priority_summary && (
              <span className="text-[10px] text-ink-500 font-mono">{batch.priority_summary}</span>
            )}
          </div>
          <div className="text-[11px] text-ink-500 mt-0.5 flex items-center gap-2 flex-wrap">
            {batch.started_at && <span title={fmtTs(batch.started_at)}>started {fmtAgo(batch.started_at)}</span>}
            {batch.completed_at && <span title={fmtTs(batch.completed_at)}>· completed {fmtAgo(batch.completed_at)}</span>}
            {appliedN > 0 && <span className="text-emerald-700">· {appliedN} applied</span>}
            {deferredN > 0 && <span className="text-amber-700">· {deferredN} deferred</span>}
            {writtenN > 0 && writtenN < batch.rec_count && <span>· {writtenN}/{batch.rec_count} written</span>}
          </div>
        </div>
        <span className="shrink-0 text-ink-400 text-sm">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-surface-divider">
          {batch.rec_items.length === 0
            ? <div className="px-3 py-3 text-xs text-ink-500 italic">(no rec items)</div>
            : batch.rec_items.map(it => (
                <RecRow key={it.rec_id} item={it} onClick={() => onPickRec(it.rec_id)} />
              ))
          }
        </div>
      )}
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Chain card

function ChainCard({
  chain, onPickRec,
}: { chain: Chain; onPickRec: (runDirBasename: string, recId: string) => void }) {
  const shipped = chain.batches.filter(b => b.status === 'completed').length
  const paused = chain.batches.filter(b => b.status === 'paused').length
  const running = chain.batches.filter(b => b.status === 'running').length
  const queued = chain.batches.filter(b => b.status === 'pending').length
  const totalBatches = chain.batches.length

  return (
    <div className="space-y-3">
      {/* Chain header */}
      <header className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="font-semibold text-ink-900 text-base capitalize">{chain.site || chain.source_agent}</h2>
            <span className="text-xs text-ink-500 font-mono truncate max-w-[260px]">{chain.source_agent}</span>
          </div>
          <div className="text-xs text-ink-500 mt-0.5 flex items-center gap-2 flex-wrap">
            <span>{chain.total_recs} recs across {totalBatches} batches of {chain.batch_size}</span>
            {chain.mtime_iso && <span>· last activity {fmtAgo(chain.mtime_iso)}</span>}
          </div>
        </div>
        <div className="flex items-center gap-1.5 text-xs">
          {shipped > 0 && <span className="status-pill bg-emerald-50 text-emerald-700 ring-emerald-200">✓ {shipped} shipped</span>}
          {running > 0 && <span className="status-pill bg-blue-50 text-blue-700 ring-blue-200"><span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" /> {running} running</span>}
          {queued > 0 && <span className="status-pill bg-ink-100 text-ink-600 ring-ink-200">⋯ {queued} queued</span>}
          {paused > 0 && <span className="status-pill bg-amber-50 text-amber-700 ring-amber-200">⏸ {paused} paused</span>}
        </div>
      </header>

      <div className="space-y-2">
        {chain.batches.map(b => (
          <BatchCard
            key={b.index}
            batch={b}
            runDirBasename={chain.run_dir_basename}
            onPickRec={(rid) => onPickRec(chain.run_dir_basename, rid)}
            defaultOpen={b.status === 'running'}
          />
        ))}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Per-rec verification (shipped tab "Verify in production" button)

type VerifyResult = {
  ok: boolean
  evidence?: unknown
  error?: string
  explanation?: string
  script_js?: string
}

async function runVerification(runDirBasename: string, recId: string): Promise<VerifyResult> {
  // Fetch the verification script the implementer (or backfill) wrote
  let doc: Awaited<ReturnType<typeof api.getRecVerificationScript>>
  try {
    doc = await api.getRecVerificationScript(runDirBasename, recId)
  } catch (e: unknown) {
    return { ok: false, error: String((e as Error)?.message || e), explanation: 'No verification script generated for this rec yet.' }
  }
  if (!doc.script_js) {
    return { ok: false, explanation: doc.explanation || 'No automatic verification — manual check needed.' }
  }
  // Build a sandbox: the script gets `proxyFetch` (server-side fetch via API).
  const proxyFetch = (url: string) => api.proxyFetch(url)
  try {
    // The script is a function literal — wrap in `(<script>)({...})` to invoke
    // Evaluation note: this trusts the doc's JS. Only reading the doc behind
    // the same auth token as the rest of the API; not user-submitted code.
    // eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
    const fn = new Function('helpers', `"use strict"; return (${doc.script_js})(helpers);`)
    const result = await Promise.race([
      fn({ proxyFetch }),
      new Promise<VerifyResult>((_, reject) => setTimeout(() => reject(new Error('verification timed out (30s)')), 30000)),
    ])
    return { ...result, explanation: doc.explanation, script_js: doc.script_js }
  } catch (e: unknown) {
    return { ok: false, error: String((e as Error)?.message || e), explanation: doc.explanation, script_js: doc.script_js }
  }
}

function VerificationModal({
  runDirBasename, recId, onClose,
}: { runDirBasename: string; recId: string; onClose: () => void }) {
  const [result, setResult] = useState<VerifyResult | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setResult(null)
    runVerification(runDirBasename, recId)
      .then(r => { if (!cancelled) { setResult(r); setLoading(false) } })
      .catch(e => {
        if (!cancelled) {
          setResult({ ok: false, error: String((e as Error)?.message || e) })
          setLoading(false)
        }
      })
    return () => { cancelled = true }
  }, [runDirBasename, recId])

  // Close on Esc only (no backdrop click). Body scroll is NOT locked —
  // the popup is a fixed centered card that doesn't need to be a true
  // modal; user can keep scrolling the page underneath if they want.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // Viewport-centered fixed popup — NO backdrop overlay. fixed inset-0 +
  // flex items-center centers on the visible viewport regardless of how
  // far the user has scrolled. pointer-events:none on the wrapper lets
  // clicks fall through to the page; pointer-events:auto on the card.
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4 pointer-events-none"
      role="dialog"
      aria-modal="false"
      aria-label="Verify in production"
    >
      <div
        className="pointer-events-auto bg-surface-card border-2 border-surface-divider rounded-xl shadow-2xl max-w-2xl w-full max-h-[75vh] overflow-auto animate-fade-in"
      >
        <header className="px-5 py-4 border-b border-surface-divider flex items-start justify-between gap-3 sticky top-0 bg-surface-card rounded-t-xl">
          <div className="flex items-center gap-3">
            {loading ? (
              <span
                className="w-4 h-4 inline-block border-2 border-blue-200 border-t-blue-600 rounded-full animate-spin"
                aria-hidden
              />
            ) : result?.ok ? (
              <span className="text-emerald-500 text-xl leading-none" aria-hidden>✓</span>
            ) : (
              <span className="text-amber-500 text-xl leading-none" aria-hidden>⚠</span>
            )}
            <div>
              <h2 className="text-base font-semibold text-ink-900">
                {loading
                  ? 'Verifying…'
                  : result?.ok
                    ? 'Verified — live in production'
                    : 'Verification failed'}
              </h2>
              <p className="text-xs text-ink-500 mt-0.5 font-mono">{recId}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-ink-500 hover:text-ink-800 text-2xl leading-none w-8 h-8 flex items-center justify-center rounded hover:bg-surface-subtle"
            aria-label="Close"
          >×</button>
        </header>
        <div className="p-5 space-y-4 text-sm">
          {loading && (
            <p className="text-ink-500 italic">
              Running the implementer-written verification script against production…
            </p>
          )}
          {!loading && result && (
            <>
              {result.error && (
                <div className="px-3 py-2 rounded bg-amber-50 border-l-4 border-amber-500 text-amber-900 text-xs font-mono">
                  {result.error}
                </div>
              )}
              {result.explanation && (
                <section>
                  <h3 className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-1">What was checked</h3>
                  <p className="text-ink-700 whitespace-pre-wrap">{result.explanation}</p>
                </section>
              )}
              {result.evidence !== undefined && (
                <section>
                  <h3 className="text-xs uppercase tracking-wide text-ink-500 font-semibold mb-1">Evidence</h3>
                  <pre className="text-xs bg-surface-subtle p-3 rounded border border-surface-divider overflow-auto whitespace-pre-wrap break-words">
                    {JSON.stringify(result.evidence, null, 2)}
                  </pre>
                </section>
              )}
              {result.script_js && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-ink-500 hover:text-ink-700">show script</summary>
                  <pre className="mt-2 bg-surface-subtle p-3 rounded border border-surface-divider overflow-auto whitespace-pre-wrap break-words font-mono">
                    {result.script_js}
                  </pre>
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// ──────────────────────────────────────────────────────────────────────────
// Filtered rec list — shown when a stat card is clicked

type CategoryFilter = 'shipped' | 'implemented' | 'queued' | 'running' | 'deferred' | null

const CATEGORY_LABELS: Record<Exclude<CategoryFilter, null>, { title: string; help: string; chip: string }> = {
  running:     { title: '⏳ Running recs',     help: 'Currently being worked on by the implementer.',                                       chip: 'text-blue-700 bg-blue-50 ring-blue-200' },
  queued:      { title: '⋯ Queued recs',       help: 'Auto-queued from email or reply; waiting for the implementer to pick them up.',         chip: 'text-ink-700 bg-ink-50 ring-ink-200' },
  implemented: { title: '✅ Implemented recs', help: 'Code committed locally — either fresh in this run, or already-in-code (verified pre-existing).', chip: 'text-emerald-700 bg-emerald-50 ring-emerald-200' },
  shipped:     { title: '🚀 Shipped recs',     help: 'Code is live in production (deployer pushed, or marked already-in-prod via pre-existing).',       chip: 'text-blue-700 bg-blue-50 ring-blue-200' },
  deferred:    { title: '⏭ Deferred recs',     help: 'Implementer declined to act — either the rec was junk data, or it required human investigation.', chip: 'text-amber-700 bg-amber-50 ring-amber-200' },
}

function reasonForRec(category: Exclude<CategoryFilter, null> | 'untouched', rec: RecItem, batchStatus: string): string {
  const summaryFirstLine = (rec.summary_first_line || '').trim()
  if (category === 'shipped') {
    if (rec.shipped_via === 'pre-existing') return 'Already in production code (verified pre-existing) — auto-flipped to shipped.'
    if (rec.shipped_tag) return `Deployed under tag ${rec.shipped_tag}${rec.shipped_at ? ` at ${rec.shipped_at}` : ''}.`
    if (rec.shipped_at) return `Shipped at ${rec.shipped_at}.`
    return summaryFirstLine || 'Shipped to production.'
  }
  if (category === 'implemented') {
    if (rec.implemented_via === 'pre-existing') return summaryFirstLine || 'Code already satisfies this rec — verified pre-existing.'
    if (rec.implemented_at) return `Code committed at ${rec.implemented_at} — awaiting deploy.`
    return summaryFirstLine || 'Code committed — awaiting deploy.'
  }
  if (category === 'deferred') {
    return summaryFirstLine || 'Deferred — see rec detail for the full rationale.'
  }
  if (category === 'queued') {
    return `Auto-queued (batch status: ${batchStatus}). Waiting for implementer pickup.`
  }
  if (category === 'running') {
    return summaryFirstLine || `Currently being worked on (batch status: ${batchStatus}).`
  }
  return summaryFirstLine || '—'
}

function FilteredRecList({
  category, recs, total, onClear, onPickRec,
}: {
  category: Exclude<CategoryFilter, null>
  recs: { chain: Chain; batch: Chain['batches'][number]; rec: RecItem; category: Exclude<CategoryFilter, null> | 'untouched' }[]
  total: number
  onClear: () => void
  onPickRec: (runDirBasename: string, recId: string) => void
}) {
  const meta = CATEGORY_LABELS[category]
  const [verifying, setVerifying] = useState<{ runDirBasename: string; recId: string } | null>(null)

  // Group by source_agent so the user can drill into "what each agent
  // has shipped/implemented/queued/running/deferred" instead of scanning
  // a flat heterogeneous list. Sub-sort agents by recent activity (most
  // recent rec timestamp first); within an agent, recs sort by rec_id desc.
  type Group = {
    agent: string; site: string; items: typeof recs;
    latestTs: string; runs: Set<string>;
  }
  const groups: Group[] = useMemo(() => {
    const map = new Map<string, Group>()
    for (const item of recs) {
      const ag = item.chain.source_agent || '(unknown agent)'
      let g = map.get(ag)
      if (!g) {
        g = { agent: ag, site: item.chain.site || '', items: [], latestTs: '', runs: new Set() }
        map.set(ag, g)
      }
      g.items.push(item)
      g.runs.add(item.chain.source_run_ts)
      const ts = item.rec.shipped_at || item.rec.implemented_at || item.chain.source_run_ts || ''
      if (ts > g.latestTs) g.latestTs = ts
    }
    // Sort: most-recent activity first
    return Array.from(map.values()).sort((a, b) => (b.latestTs || '').localeCompare(a.latestTs || ''))
  }, [recs])

  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const toggle = (agent: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(agent)) next.delete(agent); else next.add(agent)
      return next
    })
  }

  return (
    <section className="card-surface p-3 sm:p-4 space-y-3 ring-1 ring-accent-200/60">
      <header className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold text-ink-900 flex items-center gap-2">
            {meta.title}
            <span className="text-[11px] font-normal text-ink-500">({recs.length} of {total} · {groups.length} agent{groups.length === 1 ? '' : 's'})</span>
          </h2>
          <p className="text-xs text-ink-500 mt-0.5">{meta.help}</p>
        </div>
        <button onClick={onClear} className="btn-secondary !text-xs">✕ clear filter</button>
      </header>
      {verifying && (
        <VerificationModal
          runDirBasename={verifying.runDirBasename}
          recId={verifying.recId}
          onClose={() => setVerifying(null)}
        />
      )}
      {recs.length === 0 ? (
        <p className="text-sm text-ink-500 italic">No recs in this bucket.</p>
      ) : (
        <div className="space-y-3">
          {groups.map(g => {
            const isCollapsed = collapsed.has(g.agent)
            return (
              <div key={g.agent} className="rounded-md border border-surface-divider overflow-hidden bg-surface-card">
                <button
                  type="button"
                  onClick={() => toggle(g.agent)}
                  className="w-full px-3 py-2 flex items-center justify-between gap-2 bg-surface-subtle hover:bg-surface-divider/30 transition-colors text-left"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`text-ink-500 text-[10px] transition-transform inline-block ${isCollapsed ? '' : 'rotate-90'}`}>▶</span>
                    <span className="font-medium text-sm text-ink-900 truncate">{g.agent}</span>
                    {g.site && (
                      <span className="status-pill text-[10px] ring-1 ring-ink-200 bg-ink-50 text-ink-700 shrink-0">{g.site}</span>
                    )}
                    <span className="text-[11px] text-ink-500 shrink-0">
                      {g.items.length} rec{g.items.length === 1 ? '' : 's'} · {g.runs.size} run{g.runs.size === 1 ? '' : 's'}
                    </span>
                  </div>
                  {g.latestTs && (
                    <span className="text-[10px] text-ink-400 font-mono shrink-0" title={formatAbsDate(g.latestTs)}>
                      {formatRelDate(g.latestTs)}
                    </span>
                  )}
                </button>
                {!isCollapsed && (
                  <ul className="divide-y divide-surface-divider">
                    {g.items.map(({ chain, batch, rec }) => (
                      <li key={`${chain.run_dir_basename}/${rec.rec_id}`} className="hover:bg-surface-subtle transition-colors">
                        <div className="flex items-stretch">
                          <button
                            onClick={() => onPickRec(chain.run_dir_basename, rec.rec_id)}
                            className="flex-1 text-left px-3 py-2.5 flex flex-col gap-1"
                          >
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="font-mono text-[11px] text-ink-500 shrink-0">{rec.rec_id}</span>
                              {rec.kind && (
                                <span className={`status-pill text-[10px] ${priorityClass(rec.kind)} shrink-0`}>{rec.kind}</span>
                              )}
                              <span className={`status-pill text-[10px] ring-1 ${meta.chip} shrink-0`}>{category}</span>
                              <span className="flex-1 min-w-0 text-sm text-ink-800 font-medium truncate">{rec.title || '—'}</span>
                            </div>
                            <div className="text-[11px] text-ink-500 pl-1">
                              <span className="font-medium text-ink-600">why:</span> {reasonForRec(category, rec, batch.status)}
                            </div>
                            <div className="text-[10px] text-ink-400 pl-1 flex flex-wrap gap-x-2 gap-y-0.5">
                              <span>run={chain.source_run_ts.slice(0, 19)}</span>
                              <span>batch {batch.index}</span>
                              {rec.implemented_at && (
                                <span title={formatAbsDate(rec.implemented_at)}>
                                  <span className="text-emerald-600">implemented</span> {formatRelDate(rec.implemented_at)}
                                </span>
                              )}
                              {rec.shipped_at && (
                                <span title={formatAbsDate(rec.shipped_at)}>
                                  <span className="text-blue-600">shipped</span> {formatRelDate(rec.shipped_at)}
                                  {rec.shipped_tag ? ` · ${rec.shipped_tag}` : ''}
                                </span>
                              )}
                            </div>
                          </button>
                          {(category === 'shipped' || category === 'implemented') && (
                            <button
                              type="button"
                              onClick={(e) => {
                                e.preventDefault()
                                e.stopPropagation()
                                setVerifying({ runDirBasename: chain.run_dir_basename, recId: rec.rec_id })
                              }}
                              className="px-3 self-stretch text-[11px] text-blue-700 hover:bg-blue-50 hover:text-blue-900 border-l border-surface-divider whitespace-nowrap flex items-center gap-1"
                              title="Run a quick check that proves this change is live in production"
                            >
                              <span>🔍</span>
                              <span>verify</span>
                            </button>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ──────────────────────────────────────────────────────────────────────────
// Page

export default function ImplementerQueue() {
  const [chains, setChains] = useState<Chain[] | null>(null)
  const [error, setError] = useState('')
  const [refreshedAt, setRefreshedAt] = useState('')
  const [pickedRec, setPickedRec] = useState<{ runDirBasename: string; recId: string } | null>(null)
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>(null)

  const refresh = async () => {
    try {
      const r = await api.implementerBatches(20)
      setChains(r.chains)
      setRefreshedAt(new Date().toLocaleTimeString())
      setError('')
    } catch (e: unknown) {
      setError(String((e as Error)?.message || e))
    }
  }

  useEffect(() => {
    refresh()
    // Slow auto-refresh — 15s — when nothing's running, fast — 5s — when running.
    const interval = chains && chains.some(c => c.chain_status === 'running') ? 5000 : 15000
    const id = setInterval(refresh, interval)
    return () => clearInterval(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chains?.some(c => c.chain_status === 'running')])

  // Auto-target the running chain's source agent (for Live LLM)
  const liveTarget = useMemo(() => {
    const running = (chains || []).find(c => c.chain_status === 'running')
    return running?.source_agent || (chains?.[0]?.source_agent ?? '')
  }, [chains])

  // Classify each rec into exactly one bucket; precedence:
  //   shipped > implemented > deferred > running > queued > untouched
  // Returns the bucket name or null if it doesn't fit any.
  const classifyRec = (r: RecItem, batchStatus: string): CategoryFilter | 'untouched' => {
    if (r.shipped) return 'shipped'
    if (r.implemented) return 'implemented'
    if (r.applied) return 'shipped' // legacy: treat applied as shipped
    if (r.deferred) return 'deferred'
    if (batchStatus === 'running') return 'running'
    if (batchStatus === 'pending') return 'queued'
    return 'untouched'
  }

  // Aggregate counters — single bucket per rec (no double-counting).
  const totals = useMemo(() => {
    const t = { shipped: 0, implemented: 0, deferred: 0, queued: 0, running: 0, untouched: 0 }
    for (const c of (chains || [])) {
      for (const b of c.batches) {
        for (const r of b.rec_items) {
          const cat = classifyRec(r, b.status)
          t[cat as keyof typeof t]++
        }
      }
    }
    return t
  }, [chains])

  // Flatten all recs across chains/batches with rich metadata for the
  // filtered drill-down view.
  type FlatRec = {
    chain: Chain
    batch: Chain['batches'][number]
    rec: RecItem
    category: Exclude<CategoryFilter, null> | 'untouched'
  }
  const flatRecs: FlatRec[] = useMemo(() => {
    const out: FlatRec[] = []
    for (const c of (chains || [])) {
      for (const b of c.batches) {
        for (const r of b.rec_items) {
          out.push({ chain: c, batch: b, rec: r, category: classifyRec(r, b.status) as FlatRec['category'] })
        }
      }
    }
    return out
  }, [chains])

  const filteredRecs = useMemo(() => {
    if (!categoryFilter) return []
    return flatRecs.filter(f => f.category === categoryFilter)
  }, [flatRecs, categoryFilter])

  // Bucket chains by status
  const inProgressChains = (chains || []).filter(c => c.chain_status === 'running')
  const queuedChains     = (chains || []).filter(c => c.chain_status === 'queued')
  const completedChains  = (chains || []).filter(c => c.chain_status === 'completed' || c.chain_status === 'paused')

  return (
    <div className="space-y-5">
      {/* Page header */}
      <header className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-ink-900 tracking-tight">Implementer Queue</h1>
          <p className="text-xs sm:text-sm text-ink-500 mt-0.5">
            Batched dispatch chains from your email replies. Drill into any rec for details + summary.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-ink-500">
          {refreshedAt && <span>refreshed {refreshedAt}</span>}
          <button onClick={refresh} className="btn-secondary !text-xs">↻ refresh</button>
        </div>
      </header>

      {error && (
        <div className="px-4 py-2.5 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {/* Live LLM (always visible) */}
      <LivePanel targetAgent={liveTarget} />

      {/* Top stats — click any card to drill into the recs in that bucket */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 sm:gap-3">
        {([
          { label: 'Running',     value: totals.running,     color: 'text-blue-600',    bg: 'bg-blue-50/60',    key: 'running'     as const },
          { label: 'Queued',      value: totals.queued,      color: 'text-ink-700',     bg: 'bg-surface-card',  key: 'queued'      as const },
          { label: 'Implemented', value: totals.implemented, color: 'text-emerald-700', bg: 'bg-emerald-50/40', key: 'implemented' as const },
          { label: 'Shipped',     value: totals.shipped,     color: 'text-blue-700',    bg: 'bg-blue-50/40',    key: 'shipped'     as const },
          { label: 'Deferred',    value: totals.deferred,    color: 'text-amber-600',   bg: 'bg-amber-50/60',   key: 'deferred'    as const },
        ]).map(stat => {
          const active = categoryFilter === stat.key
          return (
            <button
              key={stat.label}
              onClick={() => setCategoryFilter(active ? null : stat.key)}
              className={
                `card-surface p-3 sm:p-4 text-center transition-all ${stat.bg} ` +
                `hover:ring-2 hover:ring-accent-300/40 hover:shadow-sm cursor-pointer ` +
                (active ? 'ring-2 ring-accent-500 shadow-md' : 'ring-1 ring-transparent')
              }
              aria-pressed={active}
              title={`Click to ${active ? 'clear filter' : `see all ${stat.label.toLowerCase()} recs`}`}
            >
              <div className={`text-xl sm:text-2xl font-bold ${stat.color}`}>{stat.value}</div>
              <div className="text-[10px] sm:text-xs text-ink-500 mt-0.5">{stat.label}</div>
            </button>
          )
        })}
      </div>

      {/* Filtered drill-down list — appears when a stat card is clicked */}
      {categoryFilter && (
        <FilteredRecList
          category={categoryFilter}
          recs={filteredRecs}
          total={totals[categoryFilter as keyof typeof totals]}
          onClear={() => setCategoryFilter(null)}
          onPickRec={(b, r) => setPickedRec({ runDirBasename: b, recId: r })}
        />
      )}

      {/* Skeleton loading state */}
      {chains === null && !error && (
        <div className="space-y-3">
          {/* 5 stat-card skeletons */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 sm:gap-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="card-surface p-3 sm:p-4 text-center animate-pulse" style={{ animationDelay: `${i * 50}ms` }}>
                <div className="h-7 bg-surface-subtle rounded w-12 mx-auto" />
                <div className="h-3 bg-surface-subtle rounded w-16 mx-auto mt-2" />
              </div>
            ))}
          </div>
          {/* 3 chain-card skeletons */}
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card-surface p-4 space-y-3 animate-pulse" style={{ animationDelay: `${(i + 5) * 50}ms` }}>
              <div className="flex items-center gap-2">
                <div className="h-4 bg-surface-subtle rounded w-1/3" />
                <div className="h-3 bg-surface-subtle rounded w-20 ml-auto" />
              </div>
              <div className="h-3 bg-surface-subtle rounded w-full" />
              <div className="flex gap-2">
                <div className="h-6 bg-surface-subtle rounded w-16" />
                <div className="h-6 bg-surface-subtle rounded w-20" />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {chains !== null && chains.length === 0 && (
        <div className="card-surface p-8 text-center">
          <div className="text-4xl mb-2">🛒</div>
          <h2 className="text-lg font-semibold text-ink-900">No dispatch chains yet</h2>
          <p className="text-sm text-ink-500 mt-1">
            Reply to one of your agent emails (e.g. <code className="text-xs bg-surface-subtle px-1.5 py-0.5 rounded">implement high</code>)
            and the responder will queue work here.
          </p>
        </div>
      )}

      {/* In progress */}
      {inProgressChains.length > 0 && (
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-ink-700 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
            In progress ({inProgressChains.length})
          </h2>
          {inProgressChains.map(c => (
            <ChainCard
              key={c.run_dir_basename}
              chain={c}
              onPickRec={(b, r) => setPickedRec({ runDirBasename: b, recId: r })}
            />
          ))}
        </section>
      )}

      {/* Queued */}
      {queuedChains.length > 0 && (
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-ink-700 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-ink-300" />
            Queued ({queuedChains.length})
          </h2>
          {queuedChains.map(c => (
            <ChainCard
              key={c.run_dir_basename}
              chain={c}
              onPickRec={(b, r) => setPickedRec({ runDirBasename: b, recId: r })}
            />
          ))}
        </section>
      )}

      {/* Completed / paused */}
      {completedChains.length > 0 && (
        <section className="space-y-4">
          <h2 className="text-sm font-semibold text-ink-700 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            Worked on ({completedChains.length})
          </h2>
          {completedChains.map(c => (
            <ChainCard
              key={c.run_dir_basename}
              chain={c}
              onPickRec={(b, r) => setPickedRec({ runDirBasename: b, recId: r })}
            />
          ))}
        </section>
      )}

      {/* Drill-down modal */}
      {pickedRec && (
        <RecDetailModal
          runDirBasename={pickedRec.runDirBasename}
          recId={pickedRec.recId}
          onClose={() => setPickedRec(null)}
        />
      )}
    </div>
  )
}
