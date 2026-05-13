// AI provider config: list, add, delete, set defaults, agent overrides.

import { useCallback, useEffect, useRef, useState } from 'react'

interface Provider {
  name: string
  kind: string
  base_url: string
  api_key_env: string
  api_key: string
  api_version: string
  deployment: string
  available_models: string[]
  default_model: string
  description: string
  has_key: boolean
}

interface Defaults {
  default_provider: string
  default_model: string
  agent_overrides: Record<string, { provider?: string; model?: string }>
}

interface PoolProfile {
  id: string
  home: string
  authenticated: boolean
  in_use: number
  total_uses: number
  last_used_at: string
  label: string
  state: 'ready' | 'rate-limited' | 'no-auth'
  limit_resets_at: string
  limit_last_message: string
}

interface PoolTestResult {
  done: boolean
  ok: boolean
  output: string
}

const KINDS = ['azure_openai', 'anthropic', 'ollama', 'copilot', 'openai'] as const

function token(): string {
  return localStorage.getItem('framework_api_token') ?? ''
}
function apiBase(): string {
  return import.meta.env.VITE_API_BASE_URL ?? ''
}
async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const headers = new Headers(opts?.headers)
  headers.set('Content-Type', 'application/json')
  const t = token()
  if (t) headers.set('Authorization', `Bearer ${t}`)
  const r = await fetch(`${apiBase()}${path}`, { ...opts, headers })
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }))
    throw new Error(e.detail || r.statusText)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

function relativeTime(iso: string): string {
  if (!iso) return ''
  try {
    const diff = Date.now() - new Date(iso).getTime()
    const s = Math.floor(diff / 1000)
    if (s < 60) return `${s}s ago`
    const m = Math.floor(s / 60)
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    if (h < 24) return `${h}h ago`
    return `${Math.floor(h / 24)}d ago`
  } catch { return '' }
}

type TestState = 'idle' | 'queued' | 'running' | 'done'

function ClaudeMaxSection() {
  const [profiles, setProfiles] = useState<PoolProfile[]>([])
  const [poolError, setPoolError] = useState('')
  const [testState, setTestState] = useState<Record<string, TestState>>({})
  const [testResults, setTestResults] = useState<Record<string, PoolTestResult>>({})
  const pollRefs = useRef<Record<string, ReturnType<typeof setInterval>>>({})

  const loadProfiles = useCallback(async () => {
    try {
      setProfiles(await api<PoolProfile[]>('/api/providers/claude-pool/profiles'))
      setPoolError('')
    } catch (e) {
      setPoolError(String(e))
    }
  }, [])

  useEffect(() => {
    void loadProfiles()
    const t = setInterval(loadProfiles, 15_000)
    return () => clearInterval(t)
  }, [loadProfiles])

  const runTest = async (profileId: string) => {
    setTestState(s => ({ ...s, [profileId]: 'queued' }))
    setTestResults(r => { const n = { ...r }; delete n[profileId]; return n })
    try {
      const { job_id } = await api<{ job_id: string }>(
        `/api/providers/claude-pool/test/${profileId}`, { method: 'POST' }
      )
      setTestState(s => ({ ...s, [profileId]: 'running' }))
      const started = Date.now()
      pollRefs.current[profileId] = setInterval(async () => {
        try {
          const result = await api<PoolTestResult>(
            `/api/providers/claude-pool/test-result/${job_id}`
          )
          if (result.done || Date.now() - started > 90_000) {
            clearInterval(pollRefs.current[profileId])
            setTestState(s => ({ ...s, [profileId]: 'done' }))
            setTestResults(r => ({ ...r, [profileId]: result }))
          }
        } catch { /* keep polling */ }
      }, 2000)
    } catch (e) {
      setTestState(s => ({ ...s, [profileId]: 'done' }))
      setTestResults(r => ({ ...r, [profileId]: { done: true, ok: false, output: String(e) } }))
    }
  }

  const stateBadge = (state: PoolProfile['state']) => {
    if (state === 'ready') return 'bg-status-success-bg text-status-success-fg'
    if (state === 'rate-limited') return 'bg-status-failure-bg text-status-failure-fg'
    return 'bg-surface-subtle text-ink-500'
  }

  if (poolError && profiles.length === 0) {
    return (
      <section className="bg-surface-card border border-surface-divider rounded p-4 space-y-2">
        <h2 className="text-sm font-bold text-ink-900">Claude Max Accounts</h2>
        <div className="text-xs text-ink-500 bg-surface-subtle px-3 py-2 rounded font-mono">
          {poolError}
        </div>
      </section>
    )
  }

  if (profiles.length === 0) return null

  return (
    <section className="bg-surface-card border border-surface-divider rounded p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-bold text-ink-900">Claude Max Accounts</h2>
          <p className="text-[11px] text-ink-400 mt-0.5">
            Round-robin pool for the <code className="font-mono">claude-cli</code> provider ·{' '}
            {profiles.filter(p => p.state === 'ready').length} of {profiles.length} ready
          </p>
        </div>
        <button
          onClick={loadProfiles}
          className="px-2 py-1 text-xs bg-surface-subtle hover:bg-ink-200 rounded"
        >refresh</button>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {profiles.map(p => {
          const ts = testState[p.id] ?? 'idle'
          const result = testResults[p.id]
          const busy = ts === 'queued' || ts === 'running'
          return (
            <div
              key={p.id}
              className={`border rounded p-3 space-y-2 ${
                p.state === 'ready'
                  ? 'border-surface-divider bg-surface-subtle/40'
                  : p.state === 'rate-limited'
                  ? 'border-status-failure-glow/40 bg-status-failure-bg/30'
                  : 'border-surface-divider bg-surface-subtle/20'
              }`}
            >
              {/* Header row */}
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span className="font-mono text-xs font-semibold text-ink-900">{p.id}</span>
                    {p.label && p.label !== p.id && (
                      <span className="text-[10px] text-ink-400">{p.label}</span>
                    )}
                    <span className={`text-[10px] px-1.5 py-0.5 rounded ${stateBadge(p.state)}`}>
                      {p.state}
                    </span>
                    {p.in_use > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-status-running-bg text-status-running-fg">
                        {p.in_use} in use
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] text-ink-500 mt-1 space-y-0.5 font-mono">
                    <div>{p.total_uses.toLocaleString()} runs{p.last_used_at ? ` · ${relativeTime(p.last_used_at)}` : ''}</div>
                    {p.state === 'rate-limited' && p.limit_resets_at && (
                      <div className="text-status-failure-fg">
                        resets {p.limit_resets_at.slice(0, 19).replace('T', ' ')}
                      </div>
                    )}
                    {!p.authenticated && (
                      <div className="text-ink-400 break-all leading-tight">
                        HOME={p.home} claude /login
                      </div>
                    )}
                  </div>
                </div>
                <button
                  onClick={() => runTest(p.id)}
                  disabled={busy || !p.authenticated}
                  className={`flex-shrink-0 px-2 py-1 text-xs rounded transition-colors ${
                    busy
                      ? 'bg-status-running-bg text-status-running-fg cursor-wait'
                      : p.authenticated
                      ? 'bg-surface-subtle hover:bg-status-running-bg hover:text-status-running-fg'
                      : 'bg-surface-subtle text-ink-400 opacity-40 cursor-not-allowed'
                  }`}
                >
                  {ts === 'queued' ? 'queued…' : ts === 'running' ? 'testing…' : 'test'}
                </button>
              </div>

              {/* Test result */}
              {result && (
                <div className={`rounded px-2.5 py-2 text-[11px] font-mono whitespace-pre-wrap break-words leading-relaxed ${
                  result.ok
                    ? 'bg-status-success-bg text-status-success-fg'
                    : 'bg-status-failure-bg text-status-failure-fg'
                }`}>
                  {result.done
                    ? result.output || (result.ok ? 'OK' : 'failed — no output')
                    : 'waiting for result…'}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

export default function Providers() {
  const [providers, setProviders] = useState<Provider[]>([])
  const [defaults, setDefaults] = useState<Defaults>({
    default_provider: '', default_model: '', agent_overrides: {},
  })
  const [editing, setEditing] = useState<Provider | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')

  const refresh = async () => {
    try {
      setProviders(await api<Provider[]>('/api/providers'))
      setDefaults(await api<Defaults>('/api/providers/defaults/all'))
      setError('')
    } catch (e) {
      setError(String(e))
    }
  }
  useEffect(() => { void refresh() }, [])

  const save = async (p: Provider) => {
    try {
      await api(`/api/providers/${encodeURIComponent(p.name)}`, {
        method: 'PUT', body: JSON.stringify(p),
      })
      setEditing(null); setAdding(false); refresh()
    } catch (e) { alert(`Save failed: ${e}`) }
  }
  const remove = async (name: string) => {
    if (!confirm(`Delete provider "${name}"?`)) return
    try {
      await api(`/api/providers/${encodeURIComponent(name)}`, { method: 'DELETE' })
      refresh()
    } catch (e) { alert(`Delete failed: ${e}`) }
  }
  const setDefault = async (provider_name: string, model: string) => {
    try {
      await api('/api/providers/defaults/set', {
        method: 'POST', body: JSON.stringify({ provider_name, model }),
      })
      refresh()
    } catch (e) { alert(`Set default failed: ${e}`) }
  }

  const blank = (): Provider => ({
    name: '', kind: 'azure_openai', base_url: '', api_key_env: '', api_key: '',
    api_version: '', deployment: '', available_models: [], default_model: '',
    description: '', has_key: false,
  })

  return (
    <div className="space-y-4">
      <ClaudeMaxSection />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">AI Providers</h1>
          <div className="text-sm text-ink-400">
            Default: <code className="text-status-running-fg">{defaults.default_provider || '(none)'}</code>
            {defaults.default_model && (
              <>{' '}· model <code>{defaults.default_model}</code></>
            )}
          </div>
        </div>
        <button
          onClick={() => { setAdding(true); setEditing(blank()) }}
          className="px-3 py-1.5 bg-status-running-bg hover:bg-accent-700 text-status-running-fg rounded text-sm font-semibold"
        >+ add provider</button>
      </div>

      {error && (
        <div className="px-3 py-2 bg-status-failure-bg border border-status-failure-glow/40 rounded text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {providers.length === 0 ? (
        <div className="text-ink-500 italic text-center py-12">
          No providers configured yet. Click "+ add provider" to add one.
        </div>
      ) : (
        <div className="space-y-2">
          {providers.map(p => (
            <div key={p.name} className={`bg-surface-card border border-surface-divider p-4 rounded border ${
              defaults.default_provider === p.name ? 'border-accent-500' : 'border-surface-divider'
            }`}>
              <div className="flex items-start justify-between gap-3 flex-wrap">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono font-semibold text-ink-900">{p.name}</span>
                    <span className="text-[10px] px-2 py-0.5 bg-surface-subtle rounded text-ink-600">{p.kind}</span>
                    {p.has_key ? (
                      <span className="text-[10px] px-2 py-0.5 bg-status-success-bg text-status-success-fg rounded">🔑 key configured</span>
                    ) : !p.api_key_env && (p.kind === 'claude-cli' || p.kind === 'ollama' || p.kind === 'copilot') ? (
                      <span
                        className="text-[10px] px-2 py-0.5 bg-status-success-bg text-status-success-fg rounded"
                        title={
                          p.kind === 'claude-cli'
                            ? 'Uses your local Claude CLI session (Max subscription) — no API key required'
                            : p.kind === 'ollama'
                            ? 'Local Ollama server — authentication is host-based, no API key needed'
                            : 'GitHub Copilot via copilot-api proxy — auth is handled by the proxy, no key here'
                        }
                      >🔓 no key needed</span>
                    ) : (
                      <span
                        className="text-[10px] px-2 py-0.5 bg-status-failure-bg text-status-failure-fg rounded"
                        title={`Set the ${p.api_key_env || 'API key'} env var on the framework's host (or container) and restart, OR put the inline key into this provider's config (DEV ONLY).`}
                      >⚠ no key</span>
                    )}
                    {defaults.default_provider === p.name && (
                      <span className="text-[10px] px-2 py-0.5 bg-status-running-bg text-status-running-fg rounded">★ default</span>
                    )}
                  </div>
                  {p.description && <div className="text-xs text-ink-400 mt-1">{p.description}</div>}
                  <div className="text-[11px] text-ink-500 mt-1 font-mono space-y-0.5">
                    {p.base_url && <div>base_url: {p.base_url}</div>}
                    {p.api_key_env && <div>api_key_env: ${p.api_key_env}</div>}
                    {p.deployment && <div>deployment: {p.deployment}</div>}
                    {p.default_model && <div>default_model: {p.default_model}</div>}
                  </div>
                </div>
                <div className="flex gap-1.5 flex-wrap">
                  {defaults.default_provider !== p.name && (
                    <button
                      onClick={() => setDefault(p.name, p.default_model || '')}
                      className="px-2 py-1 bg-surface-subtle hover:bg-status-running-bg hover:text-status-running-fg rounded text-xs"
                    >set default</button>
                  )}
                  <button
                    onClick={() => setEditing(p)}
                    className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded text-xs"
                  >edit</button>
                  <button
                    onClick={() => remove(p.name)}
                    className="px-2 py-1 bg-surface-subtle hover:bg-status-failure-bg hover:text-status-failure-fg rounded text-xs"
                  >delete</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {Object.keys(defaults.agent_overrides).length > 0 && (
        <section className="bg-surface-card border border-surface-divider p-4 rounded">
          <h2 className="text-xs uppercase text-ink-500 font-semibold tracking-wide mb-2">Per-agent overrides</h2>
          <div className="space-y-1 text-sm">
            {Object.entries(defaults.agent_overrides).map(([agentId, override]) => (
              <div key={agentId} className="font-mono text-xs flex gap-3">
                <span className="text-ink-600">{agentId}</span>
                <span className="text-ink-500">→</span>
                <span className="text-ink-700">{override.provider || '(no provider)'}</span>
                {override.model && <span className="text-ink-400">model: {override.model}</span>}
              </div>
            ))}
          </div>
        </section>
      )}

      {editing && (
        <ProviderEditModal
          provider={editing}
          isNew={adding}
          onSave={save}
          onCancel={() => { setEditing(null); setAdding(false) }}
        />
      )}
    </div>
  )
}

function ProviderEditModal({
  provider, isNew, onSave, onCancel,
}: { provider: Provider; isNew: boolean; onSave: (p: Provider) => void; onCancel: () => void }) {
  const [draft, setDraft] = useState(provider)
  const set = (k: keyof Provider, v: string | string[]) => setDraft(d => ({ ...d, [k]: v }))

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-surface-card border border-surface-divider border-surface-divider rounded-lg max-w-2xl w-full max-h-[90vh] overflow-auto">
        <div className="p-4 border-b border-surface-divider flex justify-between items-center">
          <h2 className="font-bold">{isNew ? 'Add Provider' : `Edit ${provider.name}`}</h2>
          <button onClick={onCancel} className="text-ink-400 hover:text-ink-900">✕</button>
        </div>
        <div className="p-4 space-y-3 text-sm">
          <Field label="name (kebab-case, unique)" disabled={!isNew}
                 value={draft.name} onChange={(v) => set('name', v)} />
          <div>
            <label className="text-xs text-ink-400 mb-1 block">kind</label>
            <select
              value={draft.kind}
              onChange={(e) => set('kind', e.target.value)}
              className="w-full px-3 py-1.5 bg-surface-subtle border-surface-divider rounded font-mono text-xs"
            >{KINDS.map(k => <option key={k} value={k}>{k}</option>)}</select>
          </div>
          <Field label="description" value={draft.description} onChange={(v) => set('description', v)} />
          <Field label={kindHelp(draft.kind, 'base_url')}
                 value={draft.base_url} onChange={(v) => set('base_url', v)} />
          <Field label="api_key_env (env var holding the key — recommended)"
                 value={draft.api_key_env} onChange={(v) => set('api_key_env', v)} />
          <Field label="api_key (inline — DEV ONLY, leave blank if api_key_env is set)"
                 value={draft.api_key} onChange={(v) => set('api_key', v)} type="password" />
          {draft.kind === 'azure_openai' && (
            <>
              <Field label="api_version" placeholder="2024-08-01-preview"
                     value={draft.api_version} onChange={(v) => set('api_version', v)} />
              <Field label="deployment (Azure deployment name — overrides 'model' on chat)"
                     value={draft.deployment} onChange={(v) => set('deployment', v)} />
            </>
          )}
          <Field label="default_model" value={draft.default_model} onChange={(v) => set('default_model', v)} />
          <Field label="available_models (comma-separated)"
                 value={draft.available_models.join(', ')}
                 onChange={(v) => set('available_models', v.split(',').map(s => s.trim()).filter(Boolean))} />
        </div>
        <div className="p-4 border-t border-surface-divider flex gap-2 justify-end">
          <button onClick={onCancel} className="px-3 py-1.5 bg-surface-subtle hover:bg-ink-200 rounded text-sm">cancel</button>
          <button onClick={() => onSave(draft)} className="px-3 py-1.5 bg-status-running-bg hover:bg-accent-700 text-status-running-fg rounded text-sm font-semibold">save</button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, value, onChange, type = 'text', disabled, placeholder }: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; disabled?: boolean; placeholder?: string;
}) {
  return (
    <div>
      <label className="text-xs text-ink-400 mb-1 block">{label}</label>
      <input
        type={type}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-1.5 bg-surface-subtle border-surface-divider rounded font-mono text-xs disabled:opacity-50"
      />
    </div>
  )
}

function kindHelp(kind: string, field: string): string {
  if (kind === 'azure_openai' && field === 'base_url')
    return 'base_url (https://<resource>.openai.azure.com)'
  if (kind === 'ollama' && field === 'base_url')
    return 'base_url (default http://localhost:11434)'
  if (kind === 'copilot' && field === 'base_url')
    return 'base_url (default http://localhost:4141/v1)'
  return field
}
