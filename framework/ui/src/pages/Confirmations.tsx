// Pending + Responded confirmations queue.
//   • "Awaiting reply" — outbound emails the user hasn't replied to yet
//   • "Responded"      — replies the responder has processed; shows
//                        WHICH recs were marked implement/skip/merge
//   • "Pending agent action approvals" — @requires_confirmation queue

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { ConfirmationRecord } from '../api/types'
import StatusBadge from '../components/StatusBadge'

type EmailRec = {
  agent_id: string; agent_name: string; request_id: string;
  subject: string; to: string[]; rec_count: number; rec_ids: string[];
  site: string; run_ts: string; sent_at: string; kind: string;
}

type RespondedRec = {
  agent_id: string; agent_name: string; request_id: string;
  subject: string; outbound_subject: string;
  site: string; run_ts: string;
  outbound_sent_at: string; responded_at: string;
  from_address: string;
  actions_recorded: number;
  actions: { action: string; rec_ids: string[]; filters: string[]; raw_line: string }[];
  rec_ids_by_action: Record<string, string[]>;
  rec_count_outbound: number;
  schema_version: string;
}

function actionChipClass(action: string): string {
  switch (action) {
    case 'implement': return 'bg-emerald-50 text-emerald-700 ring-emerald-200'
    case 'skip':      return 'bg-amber-50 text-amber-700 ring-amber-200'
    case 'merge':     return 'bg-blue-50 text-blue-700 ring-blue-200'
    default:          return 'bg-ink-100 text-ink-600 ring-ink-200'
  }
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

export default function Confirmations() {
  const [items, setItems] = useState<ConfirmationRecord[]>([])
  const [pendingEmail, setPendingEmail] = useState<EmailRec[]>([])
  const [responded, setResponded] = useState<RespondedRec[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const refresh = async () => {
    try {
      const [confirms, emails, replied] = await Promise.all([
        api.pendingConfirmations(),
        api.pendingEmailRecs().catch(() => []),
        api.respondedEmailRecs(50).catch(() => []),
      ])
      setItems(confirms)
      setPendingEmail(emails)
      setResponded(replied)
    } catch (e) { console.error(e) }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() }, [])

  const approve = async (c: ConfirmationRecord) => {
    await api.approveConfirmation(c.agent_id, c.confirmation_id, { approver: 'ui' })
    refresh()
  }
  const reject = async (c: ConfirmationRecord) => {
    await api.rejectConfirmation(c.agent_id, c.confirmation_id, { approver: 'ui' })
    refresh()
  }

  const toggleExpand = (key: string) =>
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }))

  const totalPending = items.length + pendingEmail.length

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl sm:text-2xl font-bold">Confirmations</h1>
          <div className="text-xs text-ink-500 mt-0.5 flex flex-wrap gap-x-2">
            <span>{totalPending} pending</span>
            <span className="text-ink-300">·</span>
            <span>{pendingEmail.length} awaiting reply</span>
            <span className="text-ink-300">·</span>
            <span>{responded.length} responded</span>
            <span className="text-ink-300">·</span>
            <span>{items.length} approval{items.length === 1 ? '' : 's'}</span>
          </div>
        </div>
        <button onClick={refresh} className="btn-secondary !text-xs" aria-label="Refresh">↻</button>
      </div>

      {loading && <div className="text-ink-500">Loading…</div>}

      {/* ── Awaiting reply ─────────────────────────────────────────────── */}
      {pendingEmail.length > 0 && (
        <section data-testid="pending-email-recs">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2 flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            Awaiting reply ({pendingEmail.length})
          </h2>
          <div className="space-y-2">
            {pendingEmail.map(e => (
              <div key={e.request_id} className="card-surface p-3 sm:p-4 border-l-4 border-amber-300">
                <div className="flex justify-between items-start gap-2 mb-1">
                  <Link to={`/agents/${e.agent_id}`} className="text-sm font-semibold text-ink-900 hover:text-accent-700">
                    {e.agent_name || e.agent_id}
                    {e.site && <span className="text-ink-500"> · {e.site}</span>}
                  </Link>
                  <span className="status-pill bg-amber-50 text-amber-700 ring-amber-200 text-[10px]">
                    {e.rec_count} recs
                  </span>
                </div>
                <div className="text-xs text-ink-700 break-words">{e.subject}</div>
                <div className="text-[10px] text-ink-500 font-mono mt-1 break-all">
                  request_id: {e.request_id} · sent {fmtAgo(e.sent_at)}
                </div>
                <div className="text-[11px] text-ink-600 mt-2 leading-relaxed">
                  Reply with <code className="bg-surface-subtle px-1.5 py-0.5 rounded">implement rec-001 rec-005</code>,{' '}
                  <code className="bg-surface-subtle px-1.5 py-0.5 rounded">implement high</code>,{' '}
                  <code className="bg-surface-subtle px-1.5 py-0.5 rounded">implement all</code>, or{' '}
                  <code className="bg-surface-subtle px-1.5 py-0.5 rounded">skip rec-002</code>.
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── Responded ──────────────────────────────────────────────────── */}
      {responded.length > 0 && (
        <section data-testid="responded-email-recs">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2 flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
            Responded ({responded.length})
          </h2>
          <div className="space-y-2">
            {responded.map(r => {
              const key = `${r.agent_id}-${r.request_id}`
              const isOpen = !!expanded[key]
              const actionCounts = Object.entries(r.rec_ids_by_action || {})
                .map(([action, ids]) => ({ action, count: ids.length }))
                .filter(x => x.count > 0)
              return (
                <div key={key} className="card-surface overflow-hidden border-l-4 border-emerald-300">
                  <button
                    onClick={() => toggleExpand(key)}
                    className="w-full text-left p-3 sm:p-4 hover:bg-surface-subtle transition-colors"
                    aria-expanded={isOpen}
                  >
                    <div className="flex items-start justify-between gap-2 mb-1">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Link
                            to={`/agents/${r.agent_id}`}
                            onClick={(e) => e.stopPropagation()}
                            className="text-sm font-semibold text-ink-900 hover:text-accent-700"
                          >
                            {r.agent_name || r.agent_id}
                          </Link>
                          {r.site && <span className="text-xs text-ink-500">· {r.site}</span>}
                          <span className="status-pill bg-emerald-50 text-emerald-700 ring-emerald-200 text-[10px]">
                            ✓ confirmed
                          </span>
                        </div>
                        <div className="text-xs text-ink-700 break-words mt-1">
                          {r.outbound_subject || r.subject}
                        </div>
                      </div>
                      <span className="text-ink-400 text-sm shrink-0">{isOpen ? '▾' : '▸'}</span>
                    </div>
                    <div className="flex items-center gap-2 flex-wrap mt-2 text-[11px]">
                      {actionCounts.length > 0 ? (
                        actionCounts.map(({ action, count }) => (
                          <span key={action} className={`status-pill ${actionChipClass(action)} text-[10px]`}>
                            {action}: {count}
                          </span>
                        ))
                      ) : r.actions_recorded > 0 ? (
                        <span className="text-ink-600">{r.actions_recorded} actions recorded</span>
                      ) : (
                        <span className="text-ink-500 italic">cleared (pre-existing)</span>
                      )}
                      <span className="text-ink-500 ml-auto">
                        responded {fmtAgo(r.responded_at)}
                      </span>
                    </div>
                  </button>

                  {isOpen && (
                    <div className="border-t border-surface-divider p-3 sm:p-4 bg-surface-subtle/30 space-y-3">
                      <div className="grid grid-cols-2 gap-2 text-[11px]">
                        <KV label="request_id" value={r.request_id} mono />
                        <KV label="run_ts" value={r.run_ts} mono />
                        <KV label="sent_at" value={r.outbound_sent_at} />
                        <KV label="responded_at" value={r.responded_at} />
                        <KV label="from" value={r.from_address} />
                        <KV label="recs_in_email" value={String(r.rec_count_outbound)} />
                      </div>

                      {/* Per-action breakdown with rec_ids */}
                      {(r.actions || []).map((a, i) => (
                        <div key={i} className="border border-surface-divider rounded-md bg-surface-card p-3">
                          <div className="flex items-center gap-2 flex-wrap mb-2">
                            <span className={`status-pill ${actionChipClass(a.action)} text-[10px]`}>
                              {a.action}
                            </span>
                            {a.filters && a.filters.length > 0 && (
                              <span className="text-[10px] text-ink-500 font-mono">
                                filter: {a.filters.join(', ')}
                              </span>
                            )}
                            <span className="text-[10px] text-ink-500">
                              {a.rec_ids.length} rec{a.rec_ids.length === 1 ? '' : 's'}
                            </span>
                          </div>
                          {a.raw_line && (
                            <div className="text-[11px] text-ink-600 italic mb-2 break-words">
                              "{a.raw_line}"
                            </div>
                          )}
                          {a.rec_ids.length > 0 && (
                            <div className="flex flex-wrap gap-1">
                              {a.rec_ids.map(rid => (
                                <span key={rid} className="status-pill bg-surface-subtle text-ink-700 ring-surface-divider text-[10px] font-mono">
                                  {rid}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}

                      {(r.actions || []).length === 0 && (
                        <div className="text-xs text-ink-500 italic">
                          No action breakdown available (legacy archive entry).
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {/* ── @requires_confirmation approvals ───────────────────────────── */}
      {items.length > 0 && (
        <section data-testid="pending-approvals">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2 flex items-center gap-2">
            <span>🛡</span>
            Agent action approvals ({items.length})
          </h2>
          <div className="space-y-2">
            {items.map(c => (
              <div key={`${c.agent_id}-${c.confirmation_id}`} className="card-surface p-3 sm:p-4">
                <div className="flex justify-between items-start gap-2 mb-1">
                  <Link to={`/agents/${c.agent_id}`} className="text-sm font-semibold hover:text-accent-700">
                    {c.agent_id} <span className="text-ink-500">·</span> {c.method_name}
                  </Link>
                  <StatusBadge state={c.state as 'starting' | 'running' | 'success' | 'failure' | 'blocked' | 'idle'} />
                </div>
                <div className="text-xs text-ink-600">{c.reason}</div>
                <div className="text-[10px] text-ink-500 font-mono mt-1">{c.confirmation_id} · requested {c.requested_at}</div>
                <div className="flex gap-1.5 mt-2">
                  <button onClick={() => approve(c)} className="px-3 py-1.5 bg-emerald-50 hover:bg-emerald-100 text-emerald-700 rounded text-xs font-semibold">✓ approve</button>
                  <button onClick={() => reject(c)} className="px-3 py-1.5 bg-red-50 hover:bg-red-100 text-red-700 rounded text-xs font-semibold">✕ reject</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {!loading && totalPending === 0 && responded.length === 0 && (
        <div className="text-ink-500 italic text-center py-12">
          No confirmations across all agents.
        </div>
      )}
    </div>
  )
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] text-ink-500 uppercase tracking-wide">{label}</div>
      <div className={`text-ink-700 break-all ${mono ? 'font-mono' : ''}`}>{value || '—'}</div>
    </div>
  )
}
