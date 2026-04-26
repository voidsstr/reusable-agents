// Tabbed detail view for one agent.

import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, openStatusWS } from '../api/client'
import type {
  AgentDetail as TAgentDetail, AgentLiveStatus, ChangelogEntry,
  ConfirmationRecord, Message, RunDetail,
} from '../api/types'
import StatusBadge from '../components/StatusBadge'

type TabId = 'overview' | 'directives' | 'runs' | 'messages' | 'storage' | 'confirmations' | 'changelog'

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview',      label: 'Overview' },
  { id: 'directives',    label: 'Directives' },
  { id: 'runs',          label: 'Runs' },
  { id: 'messages',      label: 'Messages' },
  { id: 'storage',       label: 'Storage' },
  { id: 'confirmations', label: 'Confirmations' },
  { id: 'changelog',     label: 'Changelog' },
]

export default function AgentDetail() {
  const { id = '' } = useParams<{ id: string }>()
  const [detail, setDetail] = useState<TAgentDetail | null>(null)
  const [liveStatus, setLiveStatus] = useState<AgentLiveStatus | null>(null)
  const [tab, setTab] = useState<TabId>('overview')
  const [error, setError] = useState('')

  const refresh = async () => {
    try {
      setDetail(await api.getAgent(id))
      setError('')
    } catch (e) {
      setError(String(e))
    }
  }
  useEffect(() => { void refresh() /* eslint-disable-next-line */ }, [id])

  useEffect(() => {
    const ws = openStatusWS(id, setLiveStatus)
    return () => { ws?.close() }
  }, [id])

  if (error) {
    return <div className="p-3 bg-glow-failure/10 border border-glow-failure/40 rounded text-glow-failure">{error}</div>
  }
  if (!detail) {
    return <div className="text-ink-500">Loading…</div>
  }
  const liveState = liveStatus?.state ?? detail.last_run_status ?? ''

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link to="/" className="text-xs text-ink-500 hover:text-ink-300">← agents</Link>
          <h1 className="text-2xl font-bold mt-1">{detail.name}</h1>
          <div className="text-xs text-ink-500 font-mono">{detail.id}</div>
          {detail.description && <div className="text-sm text-ink-300 mt-2">{detail.description}</div>}
        </div>
        <div className="flex flex-col items-end gap-2">
          <StatusBadge state={liveState} pulsing={liveState === 'running' || liveState === 'starting'} />
          <div className="flex gap-1.5">
            <button
              onClick={async () => { await api.triggerAgent(id); setTimeout(refresh, 1500) }}
              className="text-xs px-3 py-1.5 bg-glow-running/20 hover:bg-glow-running/30 text-glow-running rounded font-semibold"
              disabled={liveState === 'running'}
            >▶ Run now</button>
            <button
              onClick={async () => {
                if (detail.enabled) await api.disableAgent(id)
                else await api.enableAgent(id)
                await refresh()
              }}
              className="text-xs px-3 py-1.5 bg-ink-700 hover:bg-ink-600 rounded"
            >{detail.enabled ? '⏸ disable' : '▶ enable'}</button>
          </div>
        </div>
      </div>

      <div className="border-b border-ink-800 flex gap-1">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-sm border-b-2 transition-colors ${
              tab === t.id ? 'border-glow-running text-ink-100' : 'border-transparent text-ink-400 hover:text-ink-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && <OverviewTab detail={detail} liveStatus={liveStatus} />}
      {tab === 'directives' && <DirectivesTab detail={detail} onUpdated={refresh} />}
      {tab === 'runs' && <RunsTab agentId={id} />}
      {tab === 'messages' && <MessagesTab agentId={id} />}
      {tab === 'storage' && <StorageTab agentId={id} />}
      {tab === 'confirmations' && <ConfirmationsTab agentId={id} onChange={refresh} />}
      {tab === 'changelog' && <ChangelogTab agentId={id} />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

function OverviewTab({ detail, liveStatus }: { detail: TAgentDetail; liveStatus: AgentLiveStatus | null }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="bg-ink-800 p-4 rounded space-y-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">Live status</h2>
        {liveStatus ? (
          <>
            <div className="font-mono text-sm">{liveStatus.message || '(no message)'}</div>
            {liveStatus.current_action && (
              <div className="text-xs text-ink-400">action: <span className="text-ink-200">{liveStatus.current_action}</span></div>
            )}
            <div className="text-xs text-ink-400">progress: {(liveStatus.progress * 100).toFixed(0)}%</div>
            <div className="text-xs text-ink-400">iteration: <span className="text-ink-200">#{liveStatus.iteration_count}</span></div>
            <div className="text-xs text-ink-500">updated: {liveStatus.updated_at}</div>
          </>
        ) : (
          <div className="text-ink-500 italic">no recent status</div>
        )}
      </div>

      <div className="bg-ink-800 p-4 rounded space-y-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">Configuration</h2>
        <KV label="schedule" value={detail.cron_expr || '(manual)'} />
        <KV label="timezone" value={detail.timezone} />
        <KV label="task type" value={detail.task_type} />
        <KV label="entry" value={detail.entry_command || '(none)'} mono />
        <KV label="repo dir" value={detail.repo_dir || '—'} mono />
        <KV label="owner" value={detail.owner || '—'} />
      </div>

      <div className="bg-ink-800 p-4 rounded md:col-span-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">Capabilities</h2>
        {detail.capabilities_detail.length === 0 ? (
          <div className="text-ink-500 italic text-sm">No capabilities declared.</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
            {detail.capabilities_detail.map(c => (
              <div key={c.name} className="border border-ink-700 rounded p-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-ink-100">{c.name}</span>
                  {c.confirmation_required && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-glow-blocked/20 text-glow-blocked">
                      ⚠ confirms
                    </span>
                  )}
                  <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                    c.risk_level === 'critical' || c.risk_level === 'high'
                      ? 'bg-glow-failure/20 text-glow-failure'
                      : c.risk_level === 'medium'
                      ? 'bg-glow-blocked/20 text-glow-blocked'
                      : 'bg-ink-700 text-ink-400'
                  }`}>{c.risk_level}</span>
                </div>
                <div className="text-xs text-ink-400 mt-1">{c.description}</div>
                {c.affects.length > 0 && (
                  <div className="text-[10px] text-ink-500 mt-1">affects: {c.affects.join(', ')}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[auto_1fr] gap-2 text-xs">
      <span className="text-ink-500">{label}:</span>
      <span className={`text-ink-200 truncate ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Directives
// ---------------------------------------------------------------------------

function DirectivesTab({ detail, onUpdated }: { detail: TAgentDetail; onUpdated: () => void }) {
  const [runbook, setRunbook] = useState(detail.runbook_body ?? '')
  const [skill, setSkill] = useState(detail.skill_body ?? '')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [proposeMessage, setProposeMessage] = useState('')

  const proposeRunbookChange = async () => {
    setBusy(true)
    try {
      const result = await api.proposeDirectives(detail.id, {
        new_content: runbook, reason: reason || 'updated via UI', proposed_by: 'ui',
      })
      setProposeMessage(`Pending confirmation: ${result.confirmation_id}`)
    } catch (e) {
      alert(`Propose failed: ${e}`)
    } finally {
      setBusy(false)
    }
  }

  const saveSkillImmediate = async () => {
    // SKILL.md is a Claude Desktop config file — typically less risky than
    // AGENT.md, so we save it directly via PATCH.
    setBusy(true)
    try {
      await api.patchAgent(detail.id, { skill_body: skill })
      onUpdated()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <section className="bg-ink-800 p-4 rounded">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">
            Runbook (AGENT.md) — confirmation required
          </h2>
          <span className="text-[10px] text-ink-500 font-mono truncate">{detail.runbook_path || '(no file)'}</span>
        </div>
        <textarea
          value={runbook}
          onChange={(e) => setRunbook(e.target.value)}
          rows={20}
          className="w-full font-mono text-xs bg-ink-950 border border-ink-700 rounded p-2"
          placeholder="(no AGENT.md found)"
        />
        <div className="flex items-center gap-2 mt-2">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="reason for change…"
            className="flex-1 px-2 py-1 bg-ink-950 border border-ink-700 rounded text-xs"
          />
          <button
            onClick={proposeRunbookChange}
            disabled={busy || !runbook.trim()}
            className="px-3 py-1 bg-glow-blocked/20 hover:bg-glow-blocked/30 text-glow-blocked rounded text-xs font-semibold"
          >propose change</button>
        </div>
        {proposeMessage && (
          <div className="mt-2 text-xs text-glow-running">{proposeMessage}</div>
        )}
      </section>

      <section className="bg-ink-800 p-4 rounded">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">SKILL.md (Desktop task definition)</h2>
          <span className="text-[10px] text-ink-500 font-mono truncate">{detail.skill_path || '(no file)'}</span>
        </div>
        <textarea
          value={skill}
          onChange={(e) => setSkill(e.target.value)}
          rows={10}
          className="w-full font-mono text-xs bg-ink-950 border border-ink-700 rounded p-2"
          placeholder="(no SKILL.md)"
        />
        <button
          onClick={saveSkillImmediate}
          disabled={busy || !detail.skill_path}
          className="mt-2 px-3 py-1 bg-ink-700 hover:bg-ink-600 rounded text-xs"
        >save SKILL.md</button>
      </section>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

type RunListItem = { run_ts: string; status: string; started_at: string; ended_at?: string | null; summary: string; iteration_count: number; progress: number }
type Artifact = { key: string; name: string; ext: string; kind: 'json' | 'jsonl' | 'html' | 'markdown' | 'text' }

function RunsTab({ agentId }: { agentId: string }) {
  const [runs, setRuns] = useState<RunListItem[]>([])
  const [openRunTs, setOpenRunTs] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = async () => {
    setLoading(true)
    try { setRuns(await api.listRuns(agentId, 200)) }
    catch (e) { console.error(e) }
    finally { setLoading(false) }
  }
  useEffect(() => { void refresh() /* eslint-disable-next-line */ }, [agentId])

  if (loading && runs.length === 0) {
    return <div className="text-ink-500 italic text-center py-8">Loading runs…</div>
  }
  if (!loading && runs.length === 0) {
    return <div className="text-ink-500 italic text-center py-8">No runs yet. Hit "Run now" to trigger one.</div>
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between text-xs text-ink-400">
        <span>{runs.length} run{runs.length === 1 ? '' : 's'}</span>
        <button onClick={refresh} className="px-2 py-1 bg-ink-700 hover:bg-ink-600 rounded">↻ refresh</button>
      </div>
      {runs.map(r => (
        <div key={r.run_ts} className="bg-ink-800 rounded">
          <button
            onClick={() => setOpenRunTs(openRunTs === r.run_ts ? null : r.run_ts)}
            className="w-full p-3 flex items-center gap-3 text-left hover:bg-ink-700/50"
          >
            <StatusBadge state={r.status as any} />
            <span className="font-mono text-xs text-ink-300 flex-shrink-0">{r.run_ts}</span>
            <span className="text-xs text-ink-400 flex-1 truncate">{r.summary || '(no summary)'}</span>
            <span className="text-[10px] text-ink-500">#{r.iteration_count}</span>
            <span className="text-[10px] text-ink-500">{openRunTs === r.run_ts ? '▾' : '▸'}</span>
          </button>
          {openRunTs === r.run_ts && (
            <RunDetailPanel agentId={agentId} runTs={r.run_ts} />
          )}
        </div>
      ))}
    </div>
  )
}

function RunDetailPanel({ agentId, runTs }: { agentId: string; runTs: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [openArtifact, setOpenArtifact] = useState<Artifact | null>(null)
  const [artifactContent, setArtifactContent] = useState<string>('')

  useEffect(() => {
    api.getRun(agentId, runTs).then(setDetail).catch(console.error)
    api.runArtifacts(agentId, runTs).then(r => setArtifacts(r.artifacts)).catch(console.error)
  }, [agentId, runTs])

  const openFile = async (a: Artifact) => {
    if (openArtifact?.key === a.key) {
      setOpenArtifact(null); setArtifactContent(''); return
    }
    setOpenArtifact(a); setArtifactContent('Loading…')
    try {
      if (a.kind === 'json') {
        const res = await api.storageRead(a.key, 'json')
        setArtifactContent(JSON.stringify(res.content, null, 2))
      } else if (a.kind === 'jsonl') {
        const res = await api.storageRead(a.key, 'jsonl')
        const lines = (res.content as unknown[]).map(x => JSON.stringify(x))
        setArtifactContent(lines.join('\n'))
      } else {
        const apiBase = import.meta.env.VITE_API_BASE_URL ?? ''
        const token = localStorage.getItem('framework_api_token')
        const res = await fetch(`${apiBase}/api/storage/read?key=${encodeURIComponent(a.key)}&format=text`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        setArtifactContent(await res.text())
      }
    } catch (e) {
      setArtifactContent(String(e))
    }
  }

  // Group artifacts by category — inputs vs outputs vs reports vs other
  const groups: Record<string, Artifact[]> = {
    'Reports': [],
    'Outputs': [],
    'Inputs': [],
    'Logs / Trace': [],
    'Other': [],
  }
  for (const a of artifacts) {
    if (a.name === 'recommendations.json') groups['Reports'].push(a)
    else if (a.name === 'email-rendered.html') groups['Reports'].push(a)
    else if (a.name === 'progress.json' || a.name === 'errors.json') groups['Logs / Trace'].push(a)
    else if (a.name === 'decisions.jsonl') groups['Logs / Trace'].push(a)
    else if (a.name === 'context-summary.md') groups['Logs / Trace'].push(a)
    else if (a.name === 'pages.jsonl') groups['Inputs'].push(a)
    else if (a.name === 'competitors.json') groups['Inputs'].push(a)
    else if (a.name.startsWith('features-')) groups['Outputs'].push(a)
    else if (a.name === 'responses.json') groups['Inputs'].push(a)
    else if (a.name === 'deploy.json') groups['Outputs'].push(a)
    else groups['Other'].push(a)
  }

  return (
    <div className="p-3 border-t border-ink-700 space-y-4">
      {/* Summary header */}
      {detail && (
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div>
            <div className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide">Status</div>
            <div className="text-ink-200 mt-1"><StatusBadge state={(detail.progress as any)?.status} /></div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide">Started</div>
            <div className="text-ink-200 font-mono text-[11px] mt-1">{(detail.progress as any)?.started_at || '—'}</div>
          </div>
        </div>
      )}

      {/* Artifacts grouped */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide">Run artifacts</h3>
          <span className="text-[10px] text-ink-500">{artifacts.length} files</span>
        </div>
        {artifacts.length === 0 ? (
          <div className="text-ink-500 italic text-xs py-2">No artifacts persisted to storage for this run yet.</div>
        ) : (
          <div className="space-y-2">
            {Object.entries(groups).map(([groupName, items]) =>
              items.length === 0 ? null : (
                <div key={groupName}>
                  <div className="text-[10px] text-ink-400 mb-1 font-semibold">{groupName}</div>
                  <div className="space-y-1">
                    {items.map(a => (
                      <div key={a.key} className="bg-ink-900/60 rounded">
                        <button
                          onClick={() => openFile(a)}
                          className="w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs hover:bg-ink-700/30"
                        >
                          <span className="text-[9px] uppercase text-ink-500 font-mono w-12 flex-shrink-0">{a.kind}</span>
                          <span className="font-mono text-ink-200 flex-1 truncate">{a.name}</span>
                          <span className="text-[10px] text-ink-500">{openArtifact?.key === a.key ? '▾' : '▸'}</span>
                        </button>
                        {openArtifact?.key === a.key && (
                          a.kind === 'html' ? (
                            <iframe
                              srcDoc={artifactContent}
                              className="w-full h-96 bg-white border-t border-ink-700"
                              title={a.name}
                            />
                          ) : (
                            <pre className="whitespace-pre-wrap text-[11px] text-ink-300 font-mono bg-ink-950 p-3 border-t border-ink-700 max-h-96 overflow-auto">
                              {artifactContent}
                            </pre>
                          )
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )
            )}
          </div>
        )}
      </div>

      {/* Decisions inline (from API, not from storage list) */}
      {detail && detail.decisions && detail.decisions.length > 0 && (
        <div>
          <h3 className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide mb-1">Decisions ({detail.decisions.length})</h3>
          <div className="space-y-1 text-xs bg-ink-900/60 rounded p-2 max-h-64 overflow-auto">
            {detail.decisions.map((d, i) => (
              <div key={i} className="grid grid-cols-[auto_auto_1fr] gap-2 items-start">
                <span className="font-mono text-ink-500 text-[10px]">{d.ts.slice(11, 19)}</span>
                <span className="px-1.5 py-0.5 bg-ink-700 rounded text-[10px] text-ink-300">{d.category}</span>
                <span className="text-ink-200">{d.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Context summary */}
      {detail?.context_summary_md && (
        <div>
          <h3 className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide mb-1">Context summary</h3>
          <pre className="whitespace-pre-wrap text-xs text-ink-300 font-mono bg-ink-950 p-2 rounded max-h-72 overflow-auto">{detail.context_summary_md}</pre>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

function MessagesTab({ agentId }: { agentId: string }) {
  const [messages, setMessages] = useState<Message[]>([])
  const [unreadOnly, setUnreadOnly] = useState(true)

  const refresh = async () => {
    try { setMessages(await api.inbox(agentId, unreadOnly, 50)) }
    catch (e) { console.error(e) }
  }
  useEffect(() => { void refresh() /* eslint-disable-next-line */ }, [agentId, unreadOnly])

  const ack = async (m: Message) => {
    await api.markRead(m.message_id, agentId)
    await refresh()
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <label className="flex items-center gap-1.5 text-ink-300">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          unread only
        </label>
        <button onClick={refresh} className="px-2 py-1 bg-ink-700 hover:bg-ink-600 rounded">↻</button>
      </div>
      {messages.length === 0 ? (
        <div className="text-ink-500 italic text-center py-8">No messages.</div>
      ) : messages.map(m => (
        <div key={m.message_id} className="bg-ink-800 p-3 rounded">
          <div className="flex justify-between items-start gap-2">
            <div className="text-xs">
              <span className="font-semibold text-ink-200">{m.from}</span>
              <span className="text-ink-500"> → </span>
              <span className="text-ink-200">{m.to.join(', ')}</span>
              <span className="text-ink-500 ml-2">{m.ts}</span>
            </div>
            <button onClick={() => ack(m)} className="text-[10px] px-2 py-0.5 bg-ink-700 hover:bg-ink-600 rounded">✓ ack</button>
          </div>
          {m.subject && <div className="text-sm font-semibold mt-1">{m.subject}</div>}
          <pre className="whitespace-pre-wrap text-xs text-ink-300 font-mono bg-ink-950 p-2 rounded mt-2">
            {JSON.stringify(m.body, null, 2)}
          </pre>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

function StorageTab({ agentId }: { agentId: string }) {
  const [keys, setKeys] = useState<string[]>([])
  const [openKey, setOpenKey] = useState<string | null>(null)
  const [openContent, setOpenContent] = useState<string>('')

  const prefix = `agents/${agentId}/`

  const refresh = async () => {
    try {
      const res = await api.storageList(prefix)
      setKeys(res.keys)
    } catch (e) { console.error(e) }
  }
  useEffect(() => { void refresh() /* eslint-disable-next-line */ }, [agentId])

  const open = async (key: string) => {
    if (openKey === key) { setOpenKey(null); setOpenContent(''); return }
    setOpenKey(key)
    try {
      const ext = key.split('.').pop()
      if (ext === 'json' || ext === 'jsonl') {
        const res = await api.storageRead(key, ext as 'json' | 'jsonl')
        setOpenContent(JSON.stringify(res.content, null, 2))
      } else {
        const apiBase = import.meta.env.VITE_API_BASE_URL ?? ''
        const token = localStorage.getItem('framework_api_token')
        const res = await fetch(`${apiBase}/api/storage/read?key=${encodeURIComponent(key)}&format=text`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        setOpenContent(await res.text())
      }
    } catch (e) {
      setOpenContent(String(e))
    }
  }

  return (
    <div className="space-y-2 text-sm">
      <div className="text-xs text-ink-500 font-mono">prefix: {prefix} ({keys.length} blobs)</div>
      {keys.length === 0 ? (
        <div className="text-ink-500 italic py-4">No blobs yet for this agent.</div>
      ) : keys.map(k => (
        <div key={k} className="bg-ink-800 rounded">
          <button
            onClick={() => open(k)}
            className="w-full text-left p-2 font-mono text-xs hover:bg-ink-700/50"
          >
            {k.replace(prefix, '')}
          </button>
          {openKey === k && (
            <pre className="whitespace-pre-wrap text-[11px] text-ink-300 font-mono bg-ink-950 p-3 border-t border-ink-700 max-h-80 overflow-auto">
              {openContent}
            </pre>
          )}
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Confirmations
// ---------------------------------------------------------------------------

function ConfirmationsTab({ agentId, onChange }: { agentId: string; onChange: () => void }) {
  const [confirmations, setConfirmations] = useState<ConfirmationRecord[]>([])

  const refresh = async () => {
    try { setConfirmations(await api.pendingConfirmations(agentId)) }
    catch (e) { console.error(e) }
  }
  useEffect(() => { void refresh() /* eslint-disable-next-line */ }, [agentId])

  const approve = async (c: ConfirmationRecord) => {
    await api.approveConfirmation(c.agent_id, c.confirmation_id, { approver: 'ui' })
    await refresh(); onChange()
  }
  const reject = async (c: ConfirmationRecord) => {
    await api.rejectConfirmation(c.agent_id, c.confirmation_id, { approver: 'ui' })
    await refresh(); onChange()
  }

  return (
    <div className="space-y-2">
      {confirmations.length === 0 ? (
        <div className="text-ink-500 italic text-center py-8">No pending confirmations.</div>
      ) : confirmations.map(c => (
        <div key={c.confirmation_id} className="bg-ink-800 p-3 rounded space-y-2">
          <div className="flex justify-between items-start gap-2">
            <div className="min-w-0 flex-1">
              <div className="font-mono text-xs text-ink-100">{c.method_name}</div>
              <div className="text-xs text-ink-400 mt-1">{c.reason}</div>
            </div>
            <StatusBadge state={c.state as any} />
          </div>
          <div className="text-[10px] text-ink-500 font-mono">{c.confirmation_id}</div>
          {c.state === 'pending' && (
            <div className="flex gap-1.5">
              <button onClick={() => approve(c)} className="px-3 py-1 bg-glow-success/20 hover:bg-glow-success/30 text-glow-success rounded text-xs font-semibold">✓ approve</button>
              <button onClick={() => reject(c)} className="px-3 py-1 bg-glow-failure/20 hover:bg-glow-failure/30 text-glow-failure rounded text-xs font-semibold">✕ reject</button>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Changelog
// ---------------------------------------------------------------------------

function ChangelogTab({ agentId }: { agentId: string }) {
  const [entries, setEntries] = useState<ChangelogEntry[]>([])

  useEffect(() => {
    api.changelog(agentId).then(setEntries).catch(console.error)
  }, [agentId])

  if (entries.length === 0) {
    return <div className="text-ink-500 italic text-center py-8">No changelog entries yet.</div>
  }

  return (
    <div className="space-y-2">
      {entries.map((e, i) => (
        <div key={i} className="bg-ink-800 p-3 rounded">
          <div className="flex items-center gap-2 text-xs">
            <span className="px-2 py-0.5 bg-ink-700 rounded text-ink-300 font-mono">{e.kind}</span>
            <span className="text-ink-500 font-mono">{e.ts}</span>
            {e.release_id && <span className="font-mono text-glow-running text-[11px]">{e.release_id}</span>}
          </div>
          <div className="text-sm text-ink-200 mt-1">{e.message}</div>
          {e.commit_sha && (
            <div className="text-[10px] font-mono text-ink-500 mt-1">commit: {e.commit_sha.slice(0, 12)}</div>
          )}
          {e.files && e.files.length > 0 && (
            <div className="text-[10px] text-ink-500 mt-1">files: {e.files.join(', ')}</div>
          )}
        </div>
      ))}
    </div>
  )
}
