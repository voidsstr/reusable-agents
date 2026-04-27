// Tabbed detail view for one agent.

import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, openStatusWS } from '../api/client'
import type {
  AgentDetail as TAgentDetail, AgentLiveStatus, ChangelogEntry,
  ConfirmationRecord, Message, RunDetail, Goal,
} from '../api/types'
import StatusBadge from '../components/StatusBadge'

type TabId = 'overview' | 'live' | 'goals' | 'directives' | 'runs' | 'messages' | 'storage' | 'confirmations' | 'changelog'

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview',      label: 'Overview' },
  { id: 'live',          label: 'Live LLM' },
  { id: 'goals',         label: 'Goals' },
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
    return <div className="p-3 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-status-failure-fg">{error}</div>
  }
  if (!detail) {
    return <div className="text-ink-500 py-12 text-center">Loading…</div>
  }
  const liveState = liveStatus?.state ?? detail.last_run_status ?? ''
  const isActive = liveState === 'running' || liveState === 'starting'

  return (
    <div className={`space-y-4 ${isActive ? 'agent-detail-active' : ''}`}>
      {/* Prominent "what the agent is doing right now" banner.
          Only when state is running/starting — quietly disappears otherwise. */}
      {isActive && liveStatus && (
        <div
          data-testid="live-action-banner"
          className={`rounded-xl p-4 flex items-center gap-3 agent-active-banner border-2 shadow-card ${
            liveState === 'starting'
              ? 'bg-gradient-to-r from-status-starting-bg to-status-starting-bg/30 border-status-starting-glow/60'
              : 'bg-gradient-to-r from-status-running-bg to-status-running-bg/30 border-status-running-glow/60'
          }`}
        >
          <div className="text-3xl animate-spin" style={{ animationDuration: '2s' }}>⚙️</div>
          <div className="flex-1 min-w-0">
            <div className={`text-xs uppercase font-bold tracking-wide ${
              liveState === 'starting' ? 'text-status-starting-fg' : 'text-status-running-fg'
            }`}>
              ● {liveState === 'starting' ? 'Starting up' : 'Working now'}
            </div>
            <div className="text-base font-semibold text-ink-900 truncate">
              {liveStatus.current_action || liveStatus.message || 'Running…'}
            </div>
            {liveStatus.message && liveStatus.current_action && liveStatus.message !== liveStatus.current_action && (
              <div className="text-xs text-ink-600 mt-0.5 truncate">{liveStatus.message}</div>
            )}
          </div>
          {liveStatus.progress > 0 && liveStatus.progress < 1 && (
            <div className="flex flex-col items-end gap-1 min-w-[120px]">
              <div className="text-xs font-mono text-ink-700 font-semibold">
                {(liveStatus.progress * 100).toFixed(0)}%
              </div>
              <div className="w-32 h-2 bg-surface-divider rounded-full overflow-hidden">
                <div
                  className={`h-full transition-all rounded-full ${
                    liveState === 'starting' ? 'bg-status-starting-glow' : 'bg-status-running-glow'
                  }`}
                  style={{ width: `${(liveStatus.progress * 100).toFixed(0)}%` }}
                />
              </div>
              <div className="text-[10px] text-ink-500 font-mono">iter #{liveStatus.iteration_count}</div>
            </div>
          )}
        </div>
      )}

      <div className="flex items-start justify-between gap-4">
        <div>
          <Link to="/" className="text-xs text-ink-500 hover:text-accent-600 transition-colors">← agents</Link>
          <h1 className="text-2xl font-semibold tracking-tight text-ink-900 mt-1">{detail.name}</h1>
          <div className="text-xs text-ink-500 font-mono mt-0.5">{detail.id}</div>
          {detail.description && <div className="text-sm text-ink-600 mt-2 max-w-2xl leading-relaxed">{detail.description}</div>}
        </div>
        <div className="flex flex-col items-end gap-2">
          <StatusBadge state={liveState} pulsing={liveState === 'running' || liveState === 'starting'} />
          <div className="flex gap-1.5">
            {(() => {
              const modes = detail.runnable_modes || ['cron', 'manual']
              const manualOk = modes.includes('manual')
              if (!manualOk) {
                return (
                  <button
                    disabled
                    className="text-xs px-3 py-1.5 bg-surface-subtle text-ink-500 rounded-lg cursor-not-allowed font-medium"
                    title={`runnable_modes=${JSON.stringify(modes)} — queue-driven, dispatched by upstream agent.`}
                  >🔒 queue-driven (no manual)</button>
                )
              }
              return (
                <button
                  data-testid="run-now"
                  onClick={async () => {
                    try {
                      await api.triggerAgent(id)
                      setTimeout(refresh, 1500)
                    } catch (e: any) {
                      alert(`Trigger failed: ${e?.message || e}`)
                    }
                  }}
                  className="btn-primary"
                  disabled={liveState === 'running'}
                >▶ Run now</button>
              )
            })()}
            <button
              onClick={async () => {
                if (detail.enabled) await api.disableAgent(id)
                else await api.enableAgent(id)
                await refresh()
              }}
              className="btn-secondary"
            >{detail.enabled ? '⏸ disable' : '▶ enable'}</button>
          </div>
        </div>
      </div>

      {/* Confirmation-flow banner */}
      {detail.confirmation_flow?.enabled && (
        <ConfirmationFlowBanner
          kind={detail.confirmation_flow.kind || ''}
          description={detail.confirmation_flow.description || ''}
          ownerEmail={detail.confirmation_flow.owner_email || detail.owner}
        />
      )}

      <div className="border-b border-surface-divider flex gap-1 overflow-x-auto">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-2 text-sm border-b-2 transition-colors whitespace-nowrap ${
              tab === t.id
                ? 'border-accent-600 text-accent-700 font-semibold'
                : 'border-transparent text-ink-500 hover:text-ink-800 hover:bg-surface-subtle'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && <OverviewTab detail={detail} liveStatus={liveStatus} />}
      {tab === 'live' && <LiveLLMTab agentId={id} liveState={liveState} />}
      {tab === 'goals' && <GoalsTab agentId={id} />}
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
      <div className="bg-surface-card border border-surface-divider p-4 rounded space-y-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">Live status</h2>
        {liveStatus ? (
          <>
            <div className="font-mono text-sm">{liveStatus.message || '(no message)'}</div>
            {liveStatus.current_action && (
              <div className="text-xs text-ink-500">action: <span className="text-ink-700">{liveStatus.current_action}</span></div>
            )}
            <div className="text-xs text-ink-500">progress: {(liveStatus.progress * 100).toFixed(0)}%</div>
            <div className="text-xs text-ink-500">iteration: <span className="text-ink-700">#{liveStatus.iteration_count}</span></div>
            <div className="text-xs text-ink-500">updated: {liveStatus.updated_at}</div>
          </>
        ) : (
          <div className="text-ink-500 italic">no recent status</div>
        )}
      </div>

      <div className="bg-surface-card border border-surface-divider p-4 rounded space-y-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">Configuration</h2>
        <KV label="schedule" value={detail.cron_expr || '(manual)'} />
        <KV label="timezone" value={detail.timezone} />
        <KV label="task type" value={detail.task_type} />
        <KV label="entry" value={detail.entry_command || '(none)'} mono />
        <KV label="repo dir" value={detail.repo_dir || '—'} mono />
        <KV label="owner" value={detail.owner || '—'} />
      </div>

      <div className="bg-surface-card border border-surface-divider p-4 rounded md:col-span-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">Capabilities</h2>
        {detail.capabilities_detail.length === 0 ? (
          <div className="text-ink-500 italic text-sm">
            No capabilities declared. Code-declared capabilities (via
            <code className="text-ink-600 mx-1">framework.core.guardrails.declare()</code>)
            are surfaced here at run-time once the agent has executed at least once.
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
            {detail.capabilities_detail.map(c => (
              <div key={c.name} className="border border-ink-700 rounded p-2">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-ink-900">{c.name}</span>
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
                      : 'bg-surface-subtle text-ink-500'
                  }`}>{c.risk_level}</span>
                </div>
                <div className="text-xs text-ink-500 mt-1">{c.description}</div>
                {c.affects.length > 0 && (
                  <div className="text-[10px] text-ink-500 mt-1">affects: {c.affects.join(', ')}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Dependencies */}
      <div className="bg-surface-card border border-surface-divider p-4 rounded md:col-span-2">
        <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">
          Dependencies <span className="text-ink-600">— see <Link to="/graph" className="underline">Graph</Link> for full picture</span>
        </h2>
        {detail.depends_on.length === 0 ? (
          <div className="text-ink-500 italic text-sm">No explicit manifest dependencies. Default framework edges may still connect this agent (visible in the Graph).</div>
        ) : (
          <div className="space-y-1 text-sm">
            {detail.depends_on.map((d, i) => (
              <div key={i} className="border border-ink-700 rounded p-2 flex items-start gap-2">
                <span className="font-mono text-ink-700">→ <Link to={`/agents/${d.agent_id}`} className="underline">{d.agent_id}</Link></span>
                <span className="text-[10px] px-1.5 py-0.5 bg-surface-subtle rounded text-ink-600">{d.kind}</span>
                {d.description && <span className="text-xs text-ink-500">{d.description}</span>}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* README — repo-level docs */}
      {detail.readme_body && (
        <div className="bg-surface-card border border-surface-divider p-4 rounded md:col-span-2">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">README</h2>
          <pre className="whitespace-pre-wrap text-xs text-ink-600 font-mono bg-ink-100 p-3 rounded max-h-96 overflow-auto">
            {detail.readme_body}
          </pre>
        </div>
      )}

      {/* Metadata */}
      {detail.metadata && Object.keys(detail.metadata).length > 0 && (
        <div className="bg-surface-card border border-surface-divider p-4 rounded md:col-span-2">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">Metadata</h2>
          <pre className="whitespace-pre-wrap text-xs font-mono text-ink-600 bg-ink-100 p-3 rounded max-h-72 overflow-auto">
            {JSON.stringify(detail.metadata, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[auto_1fr] gap-2 text-xs">
      <span className="text-ink-500">{label}:</span>
      <span className={`text-ink-700 truncate ${mono ? 'font-mono' : ''}`}>{value}</span>
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
      <section className="bg-surface-card border border-surface-divider p-4 rounded">
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
          className="w-full font-mono text-xs bg-ink-100 border border-ink-700 rounded p-2"
          placeholder="(no AGENT.md found)"
        />
        <div className="flex items-center gap-2 mt-2">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="reason for change…"
            className="flex-1 px-2 py-1 bg-ink-100 border border-ink-700 rounded text-xs"
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

      <section className="bg-surface-card border border-surface-divider p-4 rounded">
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide">SKILL.md (Desktop task definition)</h2>
          <span className="text-[10px] text-ink-500 font-mono truncate">{detail.skill_path || '(no file)'}</span>
        </div>
        <textarea
          value={skill}
          onChange={(e) => setSkill(e.target.value)}
          rows={10}
          className="w-full font-mono text-xs bg-ink-100 border border-ink-700 rounded p-2"
          placeholder="(no SKILL.md)"
        />
        <button
          onClick={saveSkillImmediate}
          disabled={busy || !detail.skill_path}
          className="mt-2 px-3 py-1 bg-surface-subtle hover:bg-ink-200 rounded text-xs"
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
      <div className="flex items-center justify-between text-xs text-ink-500">
        <span>{runs.length} run{runs.length === 1 ? '' : 's'}</span>
        <button onClick={refresh} className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded">↻ refresh</button>
      </div>
      {runs.map(r => (
        <div key={r.run_ts} className="bg-surface-card border border-surface-divider rounded">
          <button
            onClick={() => setOpenRunTs(openRunTs === r.run_ts ? null : r.run_ts)}
            className="w-full p-3 flex items-center gap-3 text-left hover:bg-surface-subtle"
          >
            <StatusBadge state={r.status as any} />
            <span className="font-mono text-xs text-ink-600 flex-shrink-0">{r.run_ts}</span>
            <span className="text-xs text-ink-500 flex-1 truncate">{r.summary || '(no summary)'}</span>
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
            <div className="text-ink-700 mt-1"><StatusBadge state={(detail.progress as any)?.status} /></div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide">Started</div>
            <div className="text-ink-700 font-mono text-[11px] mt-1">{(detail.progress as any)?.started_at || '—'}</div>
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
                  <div className="text-[10px] text-ink-500 mb-1 font-semibold">{groupName}</div>
                  <div className="space-y-1">
                    {items.map(a => (
                      <div key={a.key} className="bg-surface-subtle rounded">
                        <button
                          onClick={() => openFile(a)}
                          className="w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs hover:bg-surface-subtle"
                        >
                          <span className="text-[9px] uppercase text-ink-500 font-mono w-12 flex-shrink-0">{a.kind}</span>
                          <span className="font-mono text-ink-700 flex-1 truncate">{a.name}</span>
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
                            <pre className="whitespace-pre-wrap text-[11px] text-ink-600 font-mono bg-ink-100 p-3 border-t border-ink-700 max-h-96 overflow-auto">
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
          <div className="space-y-1 text-xs bg-surface-subtle rounded p-2 max-h-64 overflow-auto">
            {detail.decisions.map((d, i) => (
              <div key={i} className="grid grid-cols-[auto_auto_1fr] gap-2 items-start">
                <span className="font-mono text-ink-500 text-[10px]">{d.ts.slice(11, 19)}</span>
                <span className="px-1.5 py-0.5 bg-surface-subtle rounded text-[10px] text-ink-600">{d.category}</span>
                <span className="text-ink-700">{d.message}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Context summary */}
      {detail?.context_summary_md && (
        <div>
          <h3 className="text-[10px] uppercase text-ink-500 font-semibold tracking-wide mb-1">Context summary</h3>
          <pre className="whitespace-pre-wrap text-xs text-ink-600 font-mono bg-ink-100 p-2 rounded max-h-72 overflow-auto">{detail.context_summary_md}</pre>
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
        <label className="flex items-center gap-1.5 text-ink-600">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          unread only
        </label>
        <button onClick={refresh} className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded">↻</button>
      </div>
      {messages.length === 0 ? (
        <div className="text-ink-500 italic text-center py-8">No messages.</div>
      ) : messages.map(m => (
        <div key={m.message_id} className="bg-surface-card border border-surface-divider p-3 rounded">
          <div className="flex justify-between items-start gap-2">
            <div className="text-xs">
              <span className="font-semibold text-ink-700">{m.from}</span>
              <span className="text-ink-500"> → </span>
              <span className="text-ink-700">{m.to.join(', ')}</span>
              <span className="text-ink-500 ml-2">{m.ts}</span>
            </div>
            <button onClick={() => ack(m)} className="text-[10px] px-2 py-0.5 bg-surface-subtle hover:bg-ink-200 rounded">✓ ack</button>
          </div>
          {m.subject && <div className="text-sm font-semibold mt-1">{m.subject}</div>}
          <pre className="whitespace-pre-wrap text-xs text-ink-600 font-mono bg-ink-100 p-2 rounded mt-2">
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
        <div key={k} className="bg-surface-card border border-surface-divider rounded">
          <button
            onClick={() => open(k)}
            className="w-full text-left p-2 font-mono text-xs hover:bg-surface-subtle"
          >
            {k.replace(prefix, '')}
          </button>
          {openKey === k && (
            <pre className="whitespace-pre-wrap text-[11px] text-ink-600 font-mono bg-ink-100 p-3 border-t border-ink-700 max-h-80 overflow-auto">
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
        <div key={c.confirmation_id} className="bg-surface-card border border-surface-divider p-3 rounded space-y-2">
          <div className="flex justify-between items-start gap-2">
            <div className="min-w-0 flex-1">
              <div className="font-mono text-xs text-ink-900">{c.method_name}</div>
              <div className="text-xs text-ink-500 mt-1">{c.reason}</div>
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
        <div key={i} className="bg-surface-card border border-surface-divider p-3 rounded">
          <div className="flex items-center gap-2 text-xs">
            <span className="px-2 py-0.5 bg-surface-subtle rounded text-ink-600 font-mono">{e.kind}</span>
            <span className="text-ink-500 font-mono">{e.ts}</span>
            {e.release_id && <span className="font-mono text-glow-running text-[11px]">{e.release_id}</span>}
          </div>
          <div className="text-sm text-ink-700 mt-1">{e.message}</div>
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


// ---------------------------------------------------------------------------
// Confirmation-flow banner
// ---------------------------------------------------------------------------

// Light-theme palette: deep label color readable on a tinted white-ish bg.
// Each entry uses a saturated bg and a darker fg from the same hue family.
const KIND_META: Record<string, {
  emoji: string; label: string;
  bg: string; fg: string; border: string; subFg: string;
}> = {
  'email-recommendations': {
    emoji: '✉',
    label: 'Email confirmation gate',
    bg: '#d1fae5', fg: '#065f46', border: '#10b981', subFg: '#047857',
  },
  'upstream-gated': {
    emoji: '⤴',
    label: 'Upstream-gated',
    bg: '#ede9fe', fg: '#5b21b6', border: '#8b5cf6', subFg: '#6d28d9',
  },
  'per-action': {
    emoji: '🛡',
    label: 'Per-action confirmation',
    bg: '#fef3c7', fg: '#92400e', border: '#f59e0b', subFg: '#b45309',
  },
  'preview-mode': {
    emoji: '👁',
    label: 'Preview mode',
    bg: '#dbeafe', fg: '#1e40af', border: '#3b82f6', subFg: '#1d4ed8',
  },
}

// ---------------------------------------------------------------------------
// Goals
// ---------------------------------------------------------------------------

function GoalsTab({ agentId }: { agentId: string }) {
  const [goals, setGoals] = useState<Goal[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.agentGoals(agentId)
      .then(d => setGoals(d.goals || []))
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [agentId])

  if (loading) return <div className="text-ink-500 italic py-8 text-center">Loading goals…</div>

  if (goals.length === 0) {
    return (
      <div className="bg-surface-card border border-surface-divider p-4 rounded text-sm text-ink-500">
        <div className="font-semibold text-ink-700 mb-1">No goals declared yet.</div>
        <div className="text-ink-500">
          Every agent in the framework should declare 3-7 long-running goals
          that periodic runs incrementally advance. Seed defaults via:
          <pre className="text-xs bg-ink-100 mt-2 p-2 rounded">
{`bash /home/voidsstr/development/reusable-agents/install/seed-default-goals.sh`}
          </pre>
          or PUT directly to <code>/api/agents/{agentId}/goals</code> with the
          schema at <code>shared/schemas/agent-goals.schema.json</code>.
        </div>
      </div>
    )
  }

  const active = goals.filter(g => g.status !== 'accomplished')
  const accomplished = goals.filter(g => g.status === 'accomplished')

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-xs text-ink-500">
        <span>{active.length} active</span>
        <span>·</span>
        <span className="text-glow-success">{accomplished.length} accomplished</span>
      </div>

      {active.length > 0 && (
        <div>
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">Active goals</h2>
          <div className="space-y-3">
            {active.map(g => <GoalCard key={g.id} goal={g} />)}
          </div>
        </div>
      )}

      {accomplished.length > 0 && (
        <div>
          <h2 className="text-xs uppercase text-glow-success font-semibold tracking-wide mb-2">
            ✓ Accomplished
          </h2>
          <div className="space-y-2">
            {accomplished.map(g => <GoalCard key={g.id} goal={g} compact />)}
          </div>
        </div>
      )}
    </div>
  )
}

function GoalCard({ goal, compact }: { goal: Goal; compact?: boolean }) {
  const m = goal.metric || {}
  const cur = m.current ?? 0
  const tgt = m.target ?? 0
  const direction = m.direction || 'increase'
  const unit = m.unit || ''
  const pct = (() => {
    if (tgt === 0 && direction === 'decrease') return cur === 0 ? 100 : 0
    if (tgt === 0) return 0
    if (direction === 'increase') return Math.min(100, Math.max(0, (cur / tgt) * 100))
    // decrease: starting from initial value (we don't track it; use cur/tgt inverse heuristic)
    if (cur <= tgt) return 100
    return Math.max(0, 100 - ((cur - tgt) / Math.max(cur, 1)) * 100)
  })()
  const accomplished = goal.status === 'accomplished'
  const sparkline = goal.progress_history?.slice(-30) || []
  const sparkMax = Math.max(...sparkline.map(p => p.value), tgt, 1)

  return (
    <div className={`bg-surface-card border border-surface-divider rounded p-3 ${accomplished ? 'border-l-4 border-glow-success' : ''}`}>
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <div className="flex items-center gap-2 min-w-0">
          {accomplished && <span className="text-glow-success text-base">✓</span>}
          <span className="text-sm font-semibold text-ink-900">{goal.title}</span>
        </div>
        <span className="font-mono text-[10px] text-ink-500 flex-shrink-0">{goal.id}</span>
      </div>
      {!compact && goal.description && (
        <div className="text-xs text-ink-500 mb-2">{goal.description}</div>
      )}
      {m.name && (
        <div className="grid grid-cols-[1fr_auto] gap-3 items-center">
          <div className="h-1.5 bg-ink-100 rounded-full overflow-hidden">
            <div
              className={`h-full ${accomplished ? 'bg-glow-success' : 'bg-glow-running'} transition-all`}
              style={{ width: `${pct.toFixed(0)}%` }}
            />
          </div>
          <div className="text-[11px] font-mono text-ink-600 whitespace-nowrap">
            {cur}{unit} {direction === 'increase' ? '↗' : '↘'} {tgt}{unit}
          </div>
        </div>
      )}
      {!compact && sparkline.length > 1 && (
        <svg width="100%" height="24" viewBox={`0 0 ${sparkline.length * 4} 24`} className="mt-2 opacity-70">
          <polyline
            points={sparkline.map((p, i) => `${i * 4},${24 - (p.value / sparkMax) * 22}`).join(' ')}
            fill="none" stroke="#38bdf8" strokeWidth="1.5"
          />
        </svg>
      )}
      {!compact && goal.directives && goal.directives.length > 0 && (
        <div className="mt-2 text-[11px] text-ink-500">
          {goal.directives.map((d, i) => <div key={i}>↳ {d}</div>)}
        </div>
      )}
      {accomplished && goal.accomplished_at && (
        <div className="text-[10px] text-ink-500 font-mono mt-1">
          accomplished {goal.accomplished_at}
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Confirmation flow banner
// ---------------------------------------------------------------------------

function ConfirmationFlowBanner({ kind, description, ownerEmail }: { kind: string; description: string; ownerEmail: string }) {
  const meta = KIND_META[kind] || {
    emoji: '🛡', label: kind || 'Confirmation gate',
    bg: '#d1fae5', fg: '#065f46', border: '#10b981', subFg: '#047857',
  }
  return (
    <div
      data-testid="confirmation-flow-banner"
      className="rounded-lg p-4 text-sm flex items-start gap-3 shadow-card"
      style={{ background: meta.bg, color: meta.fg, border: `1px solid ${meta.border}` }}
    >
      <div className="text-2xl leading-none" aria-hidden>{meta.emoji}</div>
      <div className="flex-1 min-w-0">
        <div className="font-semibold" style={{ color: meta.fg }}>{meta.label}</div>
        {description && (
          <div className="text-[13px] mt-1 leading-relaxed" style={{ color: meta.subFg }}>
            {description}
          </div>
        )}
        {ownerEmail && (
          <div className="text-[11px] mt-2 font-mono" style={{ color: meta.subFg, opacity: 0.85 }}>
            owner: {ownerEmail}
          </div>
        )}
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Live LLM tab — tail the active dispatch log + stream the implementer's
// claude --print output. Auto-refreshes every 2s while state=running.
// ---------------------------------------------------------------------------

function LiveLLMTab({ agentId, liveState }: { agentId: string; liveState: string }) {
  const [content, setContent] = useState<string>('')
  const [error, setError] = useState<string>('')
  const [logPath, setLogPath] = useState<string>('')
  const [autoTail, setAutoTail] = useState(true)
  const [tailMs, setTailMs] = useState<number>(2000)

  const isActive = liveState === 'running' || liveState === 'starting'

  useEffect(() => {
    let alive = true
    const fetchOnce = async () => {
      try {
        const res = await api.getLiveLLMOutput(agentId)
        if (!alive) return
        setContent(res.content || '')
        setLogPath(res.log_path || '')
        setError('')
      } catch (e: any) {
        if (!alive) return
        setError(e?.message || String(e))
      }
    }
    void fetchOnce()
    if (!autoTail) return
    const ms = isActive ? tailMs : Math.max(tailMs, 5000)
    const id = setInterval(fetchOnce, ms)
    return () => { alive = false; clearInterval(id) }
  }, [agentId, autoTail, tailMs, isActive])

  return (
    <div className="space-y-3">
      <div className="card-surface p-4">
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
          <div>
            <h2 className="text-sm font-semibold text-ink-900 flex items-center gap-2">
              <span aria-hidden>🧠</span> Live LLM output
              {isActive && (
                <span className="text-[10px] uppercase tracking-wide text-status-running-fg bg-status-running-bg px-1.5 py-0.5 rounded">
                  ● tailing
                </span>
              )}
            </h2>
            <div className="text-xs text-ink-500 mt-0.5">
              Real-time stdout from the implementer scope (claude --print) for this agent's most recent dispatch.
              {logPath && (
                <span className="ml-2 font-mono text-ink-500 break-all">{logPath}</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-ink-500 flex items-center gap-1">
              <input type="checkbox" checked={autoTail} onChange={e => setAutoTail(e.target.checked)} />
              auto-tail
            </label>
            <select
              value={tailMs}
              onChange={(e) => setTailMs(parseInt(e.target.value, 10))}
              className="px-2 py-1 bg-surface-card border border-surface-divider rounded-md text-xs text-ink-700"
              disabled={!autoTail}
            >
              <option value={1000}>1s</option>
              <option value={2000}>2s</option>
              <option value={5000}>5s</option>
              <option value={10000}>10s</option>
            </select>
          </div>
        </div>

        {error && (
          <div className="text-xs text-status-failure-fg bg-status-failure-bg p-2 rounded mb-3">
            {error}
          </div>
        )}

        <pre className="bg-surface-page text-emerald-200 text-[11px] font-mono p-3 rounded-lg max-h-[60vh] overflow-auto whitespace-pre-wrap leading-relaxed">
          {content || (
            <span className="text-ink-500 italic">
              No active dispatch log for this agent.{'\n\n'}
              Live output appears when the agent dispatches work to seo-implementer
              and that implementer scope is currently executing claude --print.{'\n\n'}
              The log includes claude's reasoning + tool calls + final output.
            </span>
          )}
        </pre>
      </div>
    </div>
  )
}
