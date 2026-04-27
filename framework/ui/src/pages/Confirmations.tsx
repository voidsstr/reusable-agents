// Global pending-confirmations queue + pending email-recs awaiting reply.
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

export default function Confirmations() {
  const [items, setItems] = useState<ConfirmationRecord[]>([])
  const [emailRecs, setEmailRecs] = useState<EmailRec[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = async () => {
    try {
      const [confirms, emails] = await Promise.all([
        api.pendingConfirmations(),
        api.pendingEmailRecs().catch(() => []),
      ])
      setItems(confirms)
      setEmailRecs(emails)
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

  const totalPending = items.length + emailRecs.length

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Confirmations</h1>
          <div className="text-xs text-ink-400">
            {totalPending} pending · {items.length} approval{items.length === 1 ? '' : 's'} · {emailRecs.length} email{emailRecs.length === 1 ? '' : 's'} awaiting reply
          </div>
        </div>
        <button onClick={refresh} className="px-3 py-1.5 bg-surface-card border border-surface-divider border-surface-divider rounded text-sm hover:bg-surface-subtle">↻</button>
      </div>

      {loading && <div className="text-ink-500">Loading…</div>}

      {/* Pending email-recommendations (sent → user hasn't replied yet) */}
      {emailRecs.length > 0 && (
        <section data-testid="pending-email-recs">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">
            ✉ Email recommendations awaiting reply
          </h2>
          <div className="space-y-2">
            {emailRecs.map(e => (
              <div key={e.request_id} className="bg-surface-card border border-surface-divider p-3 rounded border-l-2 border-accent-500/50">
                <div className="flex justify-between items-start gap-2 mb-1">
                  <Link to={`/agents/${e.agent_id}`} className="text-sm font-semibold hover:text-status-running-fg">
                    {e.agent_name || e.agent_id} <span className="text-ink-500">·</span> {e.site || ''}
                  </Link>
                  <span className="text-[10px] px-2 py-0.5 bg-status-running-bg text-status-running-fg rounded font-mono">
                    {e.rec_count} recs
                  </span>
                </div>
                <div className="text-xs text-ink-600 truncate">{e.subject}</div>
                <div className="text-[11px] text-ink-400 mt-1">
                  to: <span className="font-mono">{e.to.join(', ')}</span>
                </div>
                <div className="text-[10px] text-ink-500 font-mono mt-1">
                  request_id: {e.request_id} · sent {e.sent_at} · run_ts {e.run_ts}
                </div>
                <div className="text-[11px] text-ink-600 mt-2 leading-relaxed">
                  Reply to the email with <code className="bg-surface-page px-1 py-0.5 rounded">implement rec-001 rec-005</code>,{' '}
                  <code className="bg-surface-page px-1 py-0.5 rounded">implement high</code>,{' '}
                  <code className="bg-surface-page px-1 py-0.5 rounded">implement all</code>, or{' '}
                  <code className="bg-surface-page px-1 py-0.5 rounded">skip rec-002</code>. The responder-agent picks up replies every minute.
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Pending @requires_confirmation approvals */}
      {items.length > 0 && (
        <section data-testid="pending-approvals">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">
            🛡 Pending agent action approvals
          </h2>
          <div className="space-y-2">
            {items.map(c => (
              <div key={`${c.agent_id}-${c.confirmation_id}`} className="bg-surface-card border border-surface-divider p-3 rounded">
                <div className="flex justify-between items-start gap-2 mb-1">
                  <Link to={`/agents/${c.agent_id}`} className="text-sm font-semibold hover:text-status-running-fg">
                    {c.agent_id} <span className="text-ink-500">·</span> {c.method_name}
                  </Link>
                  <StatusBadge state={c.state as any} />
                </div>
                <div className="text-xs text-ink-600">{c.reason}</div>
                <div className="text-[10px] text-ink-500 font-mono mt-1">{c.confirmation_id} · requested {c.requested_at}</div>
                <div className="flex gap-1.5 mt-2">
                  <button onClick={() => approve(c)} className="px-3 py-1 bg-status-success-bg hover:bg-glow-success/30 text-status-success-fg rounded text-xs font-semibold">✓ approve</button>
                  <button onClick={() => reject(c)} className="px-3 py-1 bg-status-failure-bg hover:bg-status-failure-glow/20 text-status-failure-fg rounded text-xs font-semibold">✕ reject</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {!loading && totalPending === 0 && (
        <div className="text-ink-500 italic text-center py-12">
          No pending confirmations or email-rec replies across all agents.
        </div>
      )}
    </div>
  )
}
