// Minimal fetch-based API client. No axios — keep the bundle slim.
// The base URL is injected via VITE_API_BASE_URL at build time. If unset,
// requests go to the same origin (which works when the API is reverse-proxied
// behind the UI's host).

import type {
  AgentDetail, AgentLiveStatus, AgentSummary, ChangelogEntry,
  ConfirmationRecord, FrameworkEvent, Message, RunDetail, RunSummary,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

function getToken(): string {
  return localStorage.getItem('framework_api_token') ?? ''
}

export function setToken(token: string) {
  if (token) localStorage.setItem('framework_api_token', token)
  else localStorage.removeItem('framework_api_token')
}

async function http<T>(path: string, opts?: RequestInit): Promise<T> {
  const token = getToken()
  const headers = new Headers(opts?.headers)
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers })
  if (!res.ok) {
    let detail: string
    try { detail = (await res.json()).detail ?? res.statusText }
    catch { detail = res.statusText }
    throw new Error(`${res.status} ${detail}`)
  }
  if (res.status === 204) return undefined as T
  const text = await res.text()
  return text ? JSON.parse(text) : (undefined as T)
}

export const api = {
  // health
  health: () => http<{ status: string; storage_backend: string; auth_enabled: boolean }>('/api/health'),

  // agents
  listAgents:        () => http<AgentSummary[]>('/api/agents'),
  getAgent:          (id: string) => http<AgentDetail>(`/api/agents/${encodeURIComponent(id)}`),
  patchAgent:        (id: string, body: Partial<AgentSummary> & { runbook_body?: string; skill_body?: string }) =>
                       http<AgentSummary>(`/api/agents/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  enableAgent:       (id: string) => http<{ ok: boolean; enabled: true }>(`/api/agents/${encodeURIComponent(id)}/enable`, { method: 'POST' }),
  disableAgent:      (id: string) => http<{ ok: boolean; enabled: false }>(`/api/agents/${encodeURIComponent(id)}/disable`, { method: 'POST' }),
  deregisterAgent:   (id: string, deleteStorage = false) => http<{ ok: boolean }>(`/api/agents/${encodeURIComponent(id)}?delete_storage=${deleteStorage}`, { method: 'DELETE' }),
  triggerAgent:      (id: string) => http<{ ok: boolean; run_id: string; detail: string }>(`/api/agents/${encodeURIComponent(id)}/trigger`, { method: 'POST' }),
  discoverAgents:    (agents_dir: string) => http<{ ok: boolean; discovered: number; updated: number }>('/api/agents/discover', { method: 'POST', body: JSON.stringify({ agents_dir }) }),
  registerAgent:     (body: Record<string, unknown>) => http<AgentSummary>('/api/agents/register', { method: 'POST', body: JSON.stringify(body) }),

  // runs
  listRuns:          (id: string, limit = 20) => http<RunSummary[]>(`/api/agents/${encodeURIComponent(id)}/runs?limit=${limit}`),
  getRun:            (id: string, runTs: string) => http<RunDetail>(`/api/agents/${encodeURIComponent(id)}/runs/${runTs}`),
  runArtifacts:      (id: string, runTs: string) => http<{ agent_id: string; run_ts: string; artifacts: { key: string; name: string; ext: string; kind: 'json' | 'jsonl' | 'html' | 'markdown' | 'text' }[] }>(`/api/agents/${encodeURIComponent(id)}/runs/${runTs}/artifacts`),
  changelog:         (id: string, limit = 50) => http<ChangelogEntry[]>(`/api/agents/${encodeURIComponent(id)}/changelog?limit=${limit}`),

  // status
  status:            (id: string) => http<AgentLiveStatus>(`/api/agents/${encodeURIComponent(id)}/status`),

  // directives
  getDirectives:     (id: string) => http<{ current: Record<string, unknown>; manifest_summary: { id: string; name: string; description: string; category: string; owner: string } }>(`/api/agents/${encodeURIComponent(id)}/directives`),
  proposeDirectives: (id: string, body: { new_content: string; reason?: string; proposed_by?: string }) =>
                       http<{ ok: boolean; request_id: string; confirmation_id: string; status: string }>(`/api/agents/${encodeURIComponent(id)}/directives/propose`, { method: 'POST', body: JSON.stringify(body) }),

  // messages
  inbox:             (id: string, unread_only = true, limit = 50) => http<Message[]>(`/api/agents/${encodeURIComponent(id)}/messages?unread_only=${unread_only}&limit=${limit}`),
  sendMessage:       (body: { from_agent: string; to_agents: string[]; kind?: string; subject?: string; body?: Record<string, unknown> }) => http<{ ok: boolean; message_id: string }>('/api/messages', { method: 'POST', body: JSON.stringify(body) }),
  markRead:          (messageId: string, agentId: string) => http<{ ok: boolean }>(`/api/messages/${encodeURIComponent(messageId)}/mark-read?agent_id=${encodeURIComponent(agentId)}`, { method: 'POST' }),

  // responses
  pendingResponses:  (id: string, includeArchive = false) => http<unknown[]>(`/api/agents/${encodeURIComponent(id)}/responses?include_archive=${includeArchive}`),

  // confirmations
  pendingConfirmations:    (id?: string) => id
                             ? http<ConfirmationRecord[]>(`/api/agents/${encodeURIComponent(id)}/confirmations`)
                             : http<ConfirmationRecord[]>('/api/confirmations'),
  approveConfirmation:     (agentId: string, confirmationId: string, body: { approver?: string; notes?: string } = {}) =>
                             http<ConfirmationRecord>(`/api/confirmations/${encodeURIComponent(agentId)}/${encodeURIComponent(confirmationId)}/approve`, { method: 'POST', body: JSON.stringify(body) }),
  rejectConfirmation:      (agentId: string, confirmationId: string, body: { approver?: string; notes?: string } = {}) =>
                             http<ConfirmationRecord>(`/api/confirmations/${encodeURIComponent(agentId)}/${encodeURIComponent(confirmationId)}/reject`, { method: 'POST', body: JSON.stringify(body) }),

  // events
  events:            (since?: string, limit = 100) => http<FrameworkEvent[]>(`/api/events?limit=${limit}${since ? '&since=' + encodeURIComponent(since) : ''}`),

  // storage
  storageList:       (prefix = '') => http<{ prefix: string; keys: string[]; count: number }>(`/api/storage/list?prefix=${encodeURIComponent(prefix)}`),
  storageRead:       (key: string, format: 'auto' | 'json' | 'jsonl' | 'text' | 'bytes' = 'auto') => http<{ key: string; format: string; content: unknown }>(`/api/storage/read?key=${encodeURIComponent(key)}&format=${format}`),
}

// ---------------------------------------------------------------------------
// WebSocket helper for live status push
// ---------------------------------------------------------------------------

export function openStatusWS(agentId: string, onMessage: (s: AgentLiveStatus) => void): WebSocket | null {
  try {
    const base = (API_BASE || window.location.origin).replace(/^http/, 'ws')
    const token = getToken()
    const url = `${base}/ws/agents/${encodeURIComponent(agentId)}/status${token ? `?token=${encodeURIComponent(token)}` : ''}`
    const ws = new WebSocket(url)
    ws.onmessage = (ev) => {
      try { onMessage(JSON.parse(ev.data)) } catch { /* ignore */ }
    }
    return ws
  } catch {
    return null
  }
}
