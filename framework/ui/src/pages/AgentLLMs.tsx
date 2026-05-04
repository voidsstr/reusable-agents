// Per-agent LLM table — shows every registered agent, the effective
// provider+model that will be used on its next run, and lets the user
// install / clear an override that takes effect on subsequent runs.
//
// Effective resolution mirrors framework/core/ai_providers.ai_client_for():
//   1. agent_overrides[id]      ("override")
//   2. manifest.metadata.ai     ("manifest")
//   3. global default           ("default")
// We surface which tier wins so the user can see why an agent is on a
// given backend (e.g., the manifest says claude-cli but an override
// pinned it to copilot/gpt-4o).

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { AgentSummary } from '../api/types'

interface Provider {
  name: string
  kind: string
  default_model: string
  available_models: string[]
  has_key: boolean
}

interface Defaults {
  default_provider: string
  default_model: string
  agent_overrides: Record<string, { provider?: string; model?: string }>
}

function token(): string {
  return localStorage.getItem('framework_api_token') ?? ''
}
function apiBase(): string {
  return import.meta.env.VITE_API_BASE_URL ?? ''
}
async function rawApi<T>(path: string, opts?: RequestInit): Promise<T> {
  const headers = new Headers(opts?.headers)
  headers.set('Content-Type', 'application/json')
  const t = token()
  if (t) headers.set('Authorization', `Bearer ${t}`)
  const r = await fetch(`${apiBase()}${path}`, { ...opts, headers, credentials: 'include' })
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }))
    throw new Error(e.detail || r.statusText)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

type Source = 'override' | 'manifest' | 'default' | 'unset'

// Column-header sort keys shared between AgentLLMs (state) and
// SortableTh (display). Defined at file scope so both reference the
// same type.
type SortKey = 'id' | 'application' | 'provider' | 'model' | 'source'

interface Row {
  agent: AgentSummary
  effectiveProvider: string
  effectiveModel: string
  source: Source
  manifestProvider?: string
  manifestModel?: string
  overrideProvider?: string
  overrideModel?: string
}

export default function AgentLLMs() {
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [defaults, setDefaults] = useState<Defaults>({
    default_provider: '', default_model: '', agent_overrides: {},
  })
  const [editing, setEditing] = useState<Row | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [appFilter, setAppFilter] = useState('all')
  const [providerFilter, setProviderFilter] = useState('all')
  const [modelFilter, setModelFilter] = useState('all')
  const [sourceFilter, setSourceFilter] = useState<'all' | Source>('all')

  // Column-header sort state. null = no explicit sort (alphabetical id).
  const [sortKey, setSortKey] = useState<SortKey | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const toggleSort = (k: SortKey) => {
    if (sortKey === k) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(k); setSortDir('asc')
    }
  }

  const refresh = async () => {
    try {
      // 3 calls in parallel — was 3 + 39 sequential getAgent() follow-ups
      // (~15s waterfall). AgentSummary now carries ai_manifest_provider/
      // ai_manifest_model/ai_source so no per-agent detail fetches needed.
      const [list, provs, defs] = await Promise.all([
        api.listAgents(),
        rawApi<Provider[]>('/api/providers'),
        rawApi<Defaults>('/api/providers/defaults/all'),
      ])
      setAgents(list)
      setProviders(provs)
      setDefaults(defs)
      setError('')
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { void refresh() }, [])

  const rows: Row[] = useMemo(() => {
    return agents.map((a) => {
      const ovr = defaults.agent_overrides[a.id] || {}
      const effectiveProvider = a.ai_provider || ''
      const effectiveModel = a.ai_model || ''
      // Prefer the server-provided ai_source; fall back to client-side
      // resolution if older API version doesn't include it yet.
      let source: Source = (a.ai_source as Source) || 'unset'
      if (!a.ai_source) {
        if (ovr.provider || ovr.model) source = 'override'
        else if (a.ai_manifest_provider || a.ai_manifest_model) source = 'manifest'
        else if (defaults.default_provider) source = 'default'
      }
      return {
        agent: a,
        effectiveProvider,
        effectiveModel,
        source,
        manifestProvider: a.ai_manifest_provider || undefined,
        manifestModel: a.ai_manifest_model || undefined,
        overrideProvider: ovr.provider,
        overrideModel: ovr.model,
      }
    })
  }, [agents, defaults])

  const applications = useMemo(
    () => Array.from(new Set(agents.map(a => a.application).filter(Boolean))).sort(),
    [agents]
  )
  const providerNames = useMemo(
    () => Array.from(new Set(rows.map(r => r.effectiveProvider).filter(Boolean))).sort(),
    [rows]
  )

  const modelNames = useMemo(
    () => Array.from(new Set(rows.map(r => r.effectiveModel).filter(Boolean))).sort(),
    [rows]
  )

  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = rows
      .filter(r => appFilter === 'all' || r.agent.application === appFilter)
      .filter(r => providerFilter === 'all' || r.effectiveProvider === providerFilter)
      .filter(r => modelFilter === 'all' || r.effectiveModel === modelFilter)
      .filter(r => sourceFilter === 'all' || r.source === sourceFilter)
      .filter(r => !q
        || r.agent.id.toLowerCase().includes(q)
        || r.agent.name.toLowerCase().includes(q)
        || (r.effectiveProvider || '').toLowerCase().includes(q)
        || (r.effectiveModel || '').toLowerCase().includes(q)
        || (r.agent.application || '').toLowerCase().includes(q)
      )
    // Sort by clicked column header; default = alphabetical by id.
    const dir = sortDir === 'asc' ? 1 : -1
    const k = sortKey
    return filtered.sort((a, b) => {
      let av = ''
      let bv = ''
      switch (k) {
        case 'application': av = a.agent.application || ''; bv = b.agent.application || ''; break
        case 'provider':    av = a.effectiveProvider;       bv = b.effectiveProvider;       break
        case 'model':       av = a.effectiveModel;          bv = b.effectiveModel;          break
        case 'source':      av = a.source;                  bv = b.source;                  break
        case 'id':
        default:            av = a.agent.id;                bv = b.agent.id;                break
      }
      const cmp = av.localeCompare(bv)
      return cmp !== 0 ? cmp * dir : a.agent.id.localeCompare(b.agent.id)
    })
  }, [rows, search, appFilter, providerFilter, modelFilter, sourceFilter, sortKey, sortDir])

  const setOverride = async (agentId: string, provider: string, model: string) => {
    await rawApi('/api/providers/defaults/agent-override', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, provider, model, clear: false }),
    })
    await refresh()
  }
  const clearOverride = async (agentId: string) => {
    await rawApi('/api/providers/defaults/agent-override', {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, provider: '', model: '', clear: true }),
    })
    await refresh()
  }

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    rows.forEach(r => {
      const key = r.effectiveProvider || '(unset)'
      c[key] = (c[key] || 0) + 1
    })
    return c
  }, [rows])

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-bold">Agent LLMs</h1>
          <p className="text-sm text-ink-500 mt-0.5">
            Effective provider + model per agent. Set per-agent overrides that take effect on the next run.
            {' '}Global default: <code className="text-ink-700">{defaults.default_provider || '(unset)'}</code>
            {defaults.default_model && <> / <code className="text-ink-700">{defaults.default_model}</code></>}
          </p>
        </div>
        <button
          onClick={refresh}
          className="px-3 py-1.5 bg-surface-subtle hover:bg-ink-200 rounded text-sm"
        >refresh</button>
      </div>

      {/* Provider counts pill row — quick "where is the fleet pointed" view */}
      <div className="flex flex-wrap gap-1.5 text-xs">
        {Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([p, n]) => (
          <button
            key={p}
            onClick={() => setProviderFilter(providerFilter === p ? 'all' : p)}
            className={`px-2 py-1 rounded ${
              providerFilter === p
                ? 'bg-accent-50 text-accent-700 border border-accent-500'
                : 'bg-surface-subtle text-ink-600 hover:bg-ink-200 border border-transparent'
            }`}
          >
            <span className="font-mono">{p}</span> · {n}
          </button>
        ))}
        {providerFilter !== 'all' && (
          <button
            onClick={() => setProviderFilter('all')}
            className="px-2 py-1 text-ink-500 hover:text-ink-900"
          >clear filter</button>
        )}
      </div>

      {/* Filters — search hits id+name+app+provider+model. Per-column
          dropdowns let you pin to one value at a time. Clicking a
          column header sorts the table by that column (toggles
          asc/desc). */}
      <div className="flex flex-wrap gap-2 items-center">
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="search id, name, app, provider, model…"
          className="px-3 py-1.5 bg-surface-card border border-surface-divider rounded text-sm w-72"
        />
        <select
          value={appFilter}
          onChange={e => setAppFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-sm"
        >
          <option value="all">all applications</option>
          {applications.map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        <select
          value={providerFilter}
          onChange={e => setProviderFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-sm"
        >
          <option value="all">all providers</option>
          {providerNames.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
        <select
          value={modelFilter}
          onChange={e => setModelFilter(e.target.value)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-sm"
        >
          <option value="all">all models</option>
          {modelNames.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
        <select
          value={sourceFilter}
          onChange={e => setSourceFilter(e.target.value as 'all' | Source)}
          className="px-2 py-1.5 bg-surface-card border border-surface-divider rounded text-sm"
        >
          <option value="all">all sources</option>
          <option value="override">override</option>
          <option value="manifest">manifest</option>
          <option value="default">default</option>
          <option value="unset">unset</option>
        </select>
        {(appFilter !== 'all' || providerFilter !== 'all' || modelFilter !== 'all' || sourceFilter !== 'all' || search || sortKey) && (
          <button
            onClick={() => {
              setAppFilter('all'); setProviderFilter('all')
              setModelFilter('all'); setSourceFilter('all')
              setSearch(''); setSortKey(null); setSortDir('asc')
            }}
            className="px-2 py-1.5 text-ink-500 hover:text-ink-900 text-xs"
          >clear all</button>
        )}
        <span className="text-xs text-ink-400 ml-auto">
          {visibleRows.length} of {rows.length} agents
        </span>
      </div>

      {error && (
        <div className="px-3 py-2 bg-status-failure-bg border border-status-failure-glow/40 rounded text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-ink-400 text-sm py-8 text-center">Loading…</div>
      ) : visibleRows.length === 0 ? (
        <div className="text-ink-500 italic text-center py-12">No agents match the current filters.</div>
      ) : (
        <div className="bg-surface-card border border-surface-divider rounded overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-surface-subtle text-[11px] uppercase tracking-wide text-ink-500">
              <tr>
                <SortableTh keyName="id"          label="agent"    sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <SortableTh keyName="application" label="app"      sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} extraCls="hidden md:table-cell" />
                <SortableTh keyName="provider"    label="provider" sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <SortableTh keyName="model"       label="model"    sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} />
                <SortableTh keyName="source"      label="source"   sortKey={sortKey} sortDir={sortDir} onClick={toggleSort} extraCls="hidden lg:table-cell" />
                <th className="text-right px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map(r => (
                <tr key={r.agent.id} className="border-t border-surface-divider hover:bg-surface-subtle/40">
                  <td className="px-3 py-2 font-mono text-xs">
                    <div className="text-ink-900">{r.agent.id}</div>
                    {r.agent.name && r.agent.name !== r.agent.id && (
                      <div className="text-[10px] text-ink-400">{r.agent.name}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 hidden md:table-cell text-xs text-ink-500">{r.agent.application || '—'}</td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {r.effectiveProvider || <span className="text-ink-400 italic">unset</span>}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {r.effectiveModel || <span className="text-ink-400">—</span>}
                  </td>
                  <td className="px-3 py-2 hidden lg:table-cell">
                    <SourceBadge source={r.source} />
                    {r.source === 'override' && r.manifestProvider && (
                      r.manifestProvider !== r.overrideProvider || r.manifestModel !== r.overrideModel
                    ) && (
                      <div className="text-[10px] text-ink-400 mt-0.5 font-mono">
                        manifest: {r.manifestProvider}{r.manifestModel ? `/${r.manifestModel}` : ''}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <button
                      onClick={() => setEditing(r)}
                      className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded text-xs"
                    >override</button>
                    {r.source === 'override' && (
                      <button
                        onClick={() => { if (confirm(`Clear override for ${r.agent.id}?`)) clearOverride(r.agent.id) }}
                        className="ml-1 px-2 py-1 bg-surface-subtle hover:bg-status-failure-bg hover:text-status-failure-fg rounded text-xs"
                      >clear</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <OverrideModal
          row={editing}
          providers={providers}
          onCancel={() => setEditing(null)}
          onSave={async (provider, model) => {
            try {
              await setOverride(editing.agent.id, provider, model)
              setEditing(null)
            } catch (e) {
              alert(`Save failed: ${e}`)
            }
          }}
        />
      )}
    </div>
  )
}

function SourceBadge({ source }: { source: Source }) {
  const map: Record<Source, { label: string; cls: string; title: string }> = {
    override: {
      label: '★ override', cls: 'bg-accent-50 text-accent-700',
      title: 'A per-agent override is set in framework defaults; takes priority over the manifest.',
    },
    manifest: {
      label: 'manifest', cls: 'bg-status-running-bg text-status-running-fg',
      title: "Resolved from the agent's manifest.metadata.ai block.",
    },
    default: {
      label: 'default', cls: 'bg-surface-subtle text-ink-600',
      title: 'No override or manifest entry — using the global default.',
    },
    unset: {
      label: 'unset', cls: 'bg-status-failure-bg text-status-failure-fg',
      title: 'No provider resolved. Calls to ai_client() will fail.',
    },
  }
  const m = map[source]
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded ${m.cls}`} title={m.title}>{m.label}</span>
  )
}

function OverrideModal({
  row, providers, onCancel, onSave,
}: { row: Row; providers: Provider[]; onCancel: () => void; onSave: (provider: string, model: string) => void }) {
  const initial = row.overrideProvider || row.manifestProvider || row.effectiveProvider || ''
  const initialModel = row.overrideModel || row.manifestModel || row.effectiveModel || ''
  const [provider, setProvider] = useState(initial)
  const [model, setModel] = useState(initialModel)

  const selected = providers.find(p => p.name === provider)
  const modelOptions = selected?.available_models ?? []

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-surface-card border border-surface-divider rounded-lg max-w-lg w-full max-h-[90vh] overflow-auto">
        <div className="p-4 border-b border-surface-divider flex justify-between items-center">
          <h2 className="font-bold">Override LLM for {row.agent.id}</h2>
          <button onClick={onCancel} className="text-ink-400 hover:text-ink-900">✕</button>
        </div>
        <div className="p-4 space-y-3 text-sm">
          <div className="text-xs text-ink-500 font-mono space-y-0.5">
            <div>current effective: <span className="text-ink-700">{row.effectiveProvider || '(unset)'}{row.effectiveModel ? ` / ${row.effectiveModel}` : ''}</span></div>
            {row.manifestProvider && (
              <div>manifest declares: <span className="text-ink-700">{row.manifestProvider}{row.manifestModel ? ` / ${row.manifestModel}` : ''}</span></div>
            )}
          </div>

          <div>
            <label className="text-xs text-ink-400 mb-1 block">provider</label>
            <select
              value={provider}
              onChange={(e) => {
                const next = e.target.value
                setProvider(next)
                // Auto-fill default model when switching providers, unless user
                // already entered a custom value the new provider also lists.
                const p = providers.find(x => x.name === next)
                if (p && !p.available_models.includes(model)) {
                  setModel(p.default_model)
                }
              }}
              className="w-full px-3 py-1.5 bg-surface-subtle border-surface-divider rounded font-mono text-xs"
            >
              <option value="">(unset)</option>
              {providers.map(p => (
                <option key={p.name} value={p.name}>
                  {p.name} ({p.kind}){!p.has_key && p.kind !== 'claude-cli' && p.kind !== 'ollama' && p.kind !== 'copilot' ? ' — no key' : ''}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-xs text-ink-400 mb-1 block">model</label>
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              list={`models-${provider}`}
              placeholder={selected?.default_model || 'leave blank for provider default'}
              className="w-full px-3 py-1.5 bg-surface-subtle border-surface-divider rounded font-mono text-xs"
            />
            {modelOptions.length > 0 && (
              <datalist id={`models-${provider}`}>
                {modelOptions.map(m => <option key={m} value={m} />)}
              </datalist>
            )}
            {modelOptions.length > 0 && (
              <div className="text-[10px] text-ink-400 mt-1 flex flex-wrap gap-1">
                {modelOptions.map(m => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setModel(m)}
                    className={`px-1.5 py-0.5 rounded font-mono ${
                      m === model ? 'bg-accent-50 text-accent-700' : 'bg-surface-subtle hover:bg-ink-200'
                    }`}
                  >{m}</button>
                ))}
              </div>
            )}
          </div>

          <div className="text-[11px] text-ink-500 bg-surface-subtle px-3 py-2 rounded">
            Saving sets <code>agent_overrides[{row.agent.id}]</code> in framework defaults.
            The change applies on the next run; an in-flight run is not affected.
          </div>
        </div>
        <div className="p-4 border-t border-surface-divider flex gap-2 justify-end">
          <button onClick={onCancel} className="px-3 py-1.5 bg-surface-subtle hover:bg-ink-200 rounded text-sm">cancel</button>
          <button
            onClick={() => onSave(provider, model)}
            disabled={!provider}
            className="px-3 py-1.5 bg-status-running-bg hover:bg-accent-700 text-status-running-fg rounded text-sm font-semibold disabled:opacity-50"
          >save override</button>
        </div>
      </div>
    </div>
  )
}

// Reusable sortable column header. Click toggles sort direction; the
// arrow indicator shows current state. Lives at the bottom of the file
// so React's lazy-import boundary still works (default export above).
function SortableTh(props: {
  keyName: SortKey
  label: string
  sortKey: SortKey | null
  sortDir: 'asc' | 'desc'
  onClick: (k: SortKey) => void
  extraCls?: string
}) {
  const active = props.sortKey === props.keyName
  const arrow = !active ? '↕' : (props.sortDir === 'asc' ? '↑' : '↓')
  return (
    <th
      onClick={() => props.onClick(props.keyName)}
      className={
        `text-left px-3 py-2 cursor-pointer select-none hover:bg-ink-200/50 ` +
        (props.extraCls || '') +
        (active ? ' text-accent-700' : '')
      }
      title={`Sort by ${props.label}`}
    >
      <span>{props.label}</span>
      <span className="ml-1 text-[10px] opacity-60">{arrow}</span>
    </th>
  )
}
