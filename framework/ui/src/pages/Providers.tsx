// AI provider config: list, add, delete, set defaults, agent overrides.

import { useEffect, useState } from 'react'

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
