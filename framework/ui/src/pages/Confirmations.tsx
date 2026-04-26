// Global pending-confirmations queue across all agents.
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { ConfirmationRecord } from '../api/types'
import StatusBadge from '../components/StatusBadge'

export default function Confirmations() {
  const [items, setItems] = useState<ConfirmationRecord[]>([])
  const [loading, setLoading] = useState(true)

  const refresh = async () => {
    try { setItems(await api.pendingConfirmations()) }
    catch (e) { console.error(e) }
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

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Confirmations</h1>
        <button onClick={refresh} className="px-3 py-1.5 bg-ink-800 border border-ink-700 rounded text-sm hover:bg-ink-700">↻</button>
      </div>
      {loading ? (
        <div className="text-ink-500">Loading…</div>
      ) : items.length === 0 ? (
        <div className="text-ink-500 italic text-center py-12">No pending confirmations across all agents.</div>
      ) : (
        <div className="space-y-2">
          {items.map(c => (
            <div key={`${c.agent_id}-${c.confirmation_id}`} className="bg-ink-800 p-3 rounded">
              <div className="flex justify-between items-start gap-2 mb-1">
                <Link to={`/agents/${c.agent_id}`} className="text-sm font-semibold hover:text-glow-running">
                  {c.agent_id} <span className="text-ink-500">·</span> {c.method_name}
                </Link>
                <StatusBadge state={c.state as any} />
              </div>
              <div className="text-xs text-ink-300">{c.reason}</div>
              <div className="text-[10px] text-ink-500 font-mono mt-1">{c.confirmation_id} · requested {c.requested_at}</div>
              <div className="flex gap-1.5 mt-2">
                <button onClick={() => approve(c)} className="px-3 py-1 bg-glow-success/20 hover:bg-glow-success/30 text-glow-success rounded text-xs font-semibold">✓ approve</button>
                <button onClick={() => reject(c)} className="px-3 py-1 bg-glow-failure/20 hover:bg-glow-failure/30 text-glow-failure rounded text-xs font-semibold">✕ reject</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
