// Minimal fetch-based API client. No axios — keep the bundle slim.
// The base URL is injected via VITE_API_BASE_URL at build time. If unset,
// requests go to the same origin (which works when the API is reverse-proxied
// behind the UI's host).

import type {
  AgentDetail, AgentLiveStatus, AgentSummary, ChangelogEntry,
  ConfirmationRecord, DispatchEntry, FrameworkEvent, Goal, Message, RunDetail, RunSummary,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

function getToken(): string {
  return localStorage.getItem('framework_api_token') ?? ''
}

export function setToken(token: string) {
  if (token) localStorage.setItem('framework_api_token', token)
  else localStorage.removeItem('framework_api_token')
}

async function http<T>(path: string, opts?: (RequestInit & { timeoutMs?: number })): Promise<T> {
  const token = getToken()
  const headers = new Headers(opts?.headers)
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  // Per-request timeout — POSTs that wait on a Container App cold-start can
  // take up to 60s on the first request after idle. Default 90s; callers
  // pass opts.timeoutMs to override.
  const timeoutMs = opts?.timeoutMs ?? 90_000
  const ac = new AbortController()
  const timer = setTimeout(() => ac.abort(), timeoutMs)
  // credentials:'include' so the OAuth session cookie is sent on cross-origin
  // dev (UI on :8091, API on :8090). Same-origin (prod) is unaffected.
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...opts, headers, credentials: 'include', signal: ac.signal,
    })
  } finally {
    clearTimeout(timer)
  }
  if (res.status === 401) {
    // Browser flow: bounce to Google login. Skip if we're already on the
    // login page or this is the /me probe (handled by caller).
    if (!path.startsWith('/api/auth/') && typeof window !== 'undefined') {
      const next = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.href = `${API_BASE}/api/auth/google/login?next=${next}`
      // Throw so callers see a rejected promise rather than undefined.
      throw new Error('401 redirecting to login')
    }
  }
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
  listAllRuns:       (params: { limit?: number; offset?: number; agent_id?: string; status?: string; application?: string; category?: string; since?: string; q?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.limit !== undefined)   qs.set('limit',   String(params.limit))
    if (params.offset !== undefined)  qs.set('offset',  String(params.offset))
    if (params.agent_id)              qs.set('agent_id', params.agent_id)
    if (params.status)                qs.set('status', params.status)
    if (params.application)           qs.set('application', params.application)
    if (params.category)              qs.set('category', params.category)
    if (params.since)                 qs.set('since', params.since)
    if (params.q)                     qs.set('q', params.q)
    const s = qs.toString()
    return http<{ total: number; limit: number; offset: number; runs: (RunSummary & { agent_name?: string; application?: string; category?: string })[] }>(`/api/runs${s ? '?' + s : ''}`)
  },
  getRun:            (id: string, runTs: string) => http<RunDetail>(`/api/agents/${encodeURIComponent(id)}/runs/${runTs}`),
  runArtifacts:      (id: string, runTs: string) => http<{ agent_id: string; run_ts: string; artifacts: { key: string; name: string; ext: string; kind: 'json' | 'jsonl' | 'html' | 'markdown' | 'text' }[] }>(`/api/agents/${encodeURIComponent(id)}/runs/${runTs}/artifacts`),
  changelog:         (id: string, limit = 50) => http<ChangelogEntry[]>(`/api/agents/${encodeURIComponent(id)}/changelog?limit=${limit}`),

  // status
  status:            (id: string) => http<AgentLiveStatus>(`/api/agents/${encodeURIComponent(id)}/status`),

  // live LLM output — tails the most recent dispatch log for the agent
  getLiveLLMOutput:  (id: string) => http<{
    agent_id: string;
    source?: string;          // 'azure-live-blob' | 'framework-storage' | 'local-fs' | 'none'
    run_ts?: string;
    log_path: string;
    content: string;
    is_active?: boolean;      // server-reported "is this run currently writing?"
    started_at?: string;
    updated_at?: string;
    tail_bytes: number;
    mtime: string | null;
  }>(`/api/agents/${encodeURIComponent(id)}/live-llm-output`),

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
  pendingEmailRecs:        () => http<{
    agent_id: string; agent_name: string; request_id: string;
    subject: string; to: string[]; rec_count: number; rec_ids: string[];
    site: string; run_ts: string; sent_at: string; kind: string;
  }[]>('/api/confirmations/pending-emails'),
  respondedEmailRecs:      (limit = 100) => http<{
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
  }[]>(`/api/confirmations/responded-emails?limit=${limit}`),
  approveConfirmation:     (agentId: string, confirmationId: string, body: { approver?: string; notes?: string } = {}) =>
                             http<ConfirmationRecord>(`/api/confirmations/${encodeURIComponent(agentId)}/${encodeURIComponent(confirmationId)}/approve`, { method: 'POST', body: JSON.stringify(body) }),
  rejectConfirmation:      (agentId: string, confirmationId: string, body: { approver?: string; notes?: string } = {}) =>
                             http<ConfirmationRecord>(`/api/confirmations/${encodeURIComponent(agentId)}/${encodeURIComponent(confirmationId)}/reject`, { method: 'POST', body: JSON.stringify(body) }),

  // events
  events:            (since?: string, limit = 100) => http<FrameworkEvent[]>(`/api/events?limit=${limit}${since ? '&since=' + encodeURIComponent(since) : ''}`),

  // storage
  storageList:       (prefix = '') => http<{ prefix: string; keys: string[]; count: number }>(`/api/storage/list?prefix=${encodeURIComponent(prefix)}`),
  storageRead:       (key: string, format: 'auto' | 'json' | 'jsonl' | 'text' | 'bytes' = 'auto') => http<{ key: string; format: string; content: unknown }>(`/api/storage/read?key=${encodeURIComponent(key)}&format=${format}`),

  // goals
  agentGoals:        (id: string) => http<{ schema_version: string; agent_id: string; updated_at?: string; goals: Goal[] }>(`/api/agents/${encodeURIComponent(id)}/goals`),
  putAgentGoals:     (id: string, goals: Goal[]) => http<{ schema_version: string; goals: Goal[] }>(`/api/agents/${encodeURIComponent(id)}/goals`, { method: 'PUT', body: JSON.stringify({ goals }) }),
  postGoalProgress:  (id: string, goalId: string, body: { value: number; run_ts?: string; note?: string; accomplished?: boolean }) => http<unknown>(`/api/agents/${encodeURIComponent(id)}/goals/${encodeURIComponent(goalId)}/progress`, { method: 'POST', body: JSON.stringify(body) }),
  goalsAccomplished: (id: string) => http<{ entries: { ts: string; goal_id: string; title: string; value: number }[] }>(`/api/agents/${encodeURIComponent(id)}/goals/accomplished`),
  goalsTimeseries: (id: string, limitRuns = 60) => http<{
    agent_id: string
    runs_scanned: number
    goal_count: number
    goals: {
      goal_id: string
      description: string
      target_metric: string
      baseline?: number | null
      target?: number | null
      from_rec?: string
      is_top5_goal?: boolean
      is_revenue_goal?: boolean
      rationale?: string
      check_by?: string
      points: { ts: string; run_ts: string; current?: number | null; progress_pct?: number | null; status?: string }[]
    }[]
    annotations: { ts: string; rec_id: string; title: string; goal_id: string; kind: 'shipped' | 'implemented' }[]
  }>(`/api/agents/${encodeURIComponent(id)}/goals/timeseries?limit_runs=${limitRuns}`),

  /** Fast pre-aggregated cache (preferred over timeseries — single storage read) */
  goalsCache: (id: string) => http<{
    agent_id: string
    updated_at: string
    goals: Record<string, {
      points: { ts: string; value: number; run_ts?: string }[]
      latest_value?: number
      latest_ts?: string
    }>
    definitions_only?: boolean
  }>(`/api/agents/${encodeURIComponent(id)}/goals/cache`),

  /** High-resolution per-goal progress (used when expanding a goal's chart) */
  goalProgress: (id: string, goalId: string, limit = 500) => http<{
    agent_id: string
    goal_id: string
    points: { ts: string; value: number; run_ts?: string; note?: string }[]
  }>(`/api/agents/${encodeURIComponent(id)}/goals/${encodeURIComponent(goalId)}/progress?limit=${limit}`),

  // LLM usage — aggregate across all agents
  llmUsage: (days?: number) => http<{
    as_of: string
    filtered_days: number | null
    by_provider_model: {
      provider: string
      kind_provider: string
      model: string
      calls: number
      input_tokens: number
      output_tokens: number
      est_cost_usd: number
      agents: string[]
      first_ts: string
      last_ts: string
      is_zero_billed: boolean
    }[]
    by_agent: {
      agent_id: string
      calls: number
      input_tokens: number
      output_tokens: number
      est_cost_usd: number
    }[]
    by_day: {
      date: string
      calls: number
      input_tokens: number
      output_tokens: number
      est_cost_usd: number
    }[]
    totals: { calls: number; input_tokens: number; output_tokens: number; est_cost_usd: number }
  }>(`/api/llm/usage${days ? `?days=${days}` : ''}`),

  llmUsageRefresh: () => http<unknown>('/api/llm/usage/refresh', { method: 'POST' }),

  // implementer dispatch queue
  implementerQueue: (limit = 20) => http<{
    pending: { agent_id: string; request_id?: string; site?: string; from_run?: string; rec_ids?: string[]; action?: string; ts?: string; _key?: string }[];
    pending_total: number;
    dispatches: DispatchEntry[];
    // Rec-memory backlog — open proposals every producer agent has emitted
    // but the implementer hasn't shipped yet. Keyed by agent_id.
    // Populated server-side from agents/<id>/state/accumulator.json.
    accumulator_by_agent?: Record<string, {
      id: string
      title: string
      rec_type: string
      severity: string
      first_seen_at: string
      last_seen_at: string
      seen_count: number
    }[]>
    accumulator_total?: number
    // Phase-2 truth fields (post-auto-queue elimination): the real
    // backlog lives in producer recommendations.json. open_recs_total
    // mirrors the lifetime-stats pending bucket; running_dispatches
    // counts active scopes.
    open_recs_total?: number
    open_recs_by_agent?: Record<string, number>
    running_dispatches?: number
  }>(`/api/implementer/queue?limit=${limit}`),
  implementerDispatches: (limit = 20) => http<DispatchEntry[]>(`/api/implementer/dispatches?limit=${limit}`),
  getDispatchLog: (dispatchId: string, tailBytes = 32768) => http<DispatchEntry & { content: string }>(`/api/implementer/dispatches/${encodeURIComponent(dispatchId)}/log?tail_bytes=${tailBytes}`),
  // Batched dispatch chains (one per "implement all"-style reply that
  // got split into chunks of size max_recs_per_run)
  implementerBatches: (limit = 20) => http<{
    chains: {
      run_dir_basename: string
      dispatch_run_ts: string
      source_run_ts: string
      source_agent: string
      site: string
      batch_size: number
      total_recs: number
      chain_status: string  // 'running' | 'queued' | 'paused' | 'completed'
      mtime_iso: string
      batches: {
        index: number
        status: string  // 'pending' | 'running' | 'completed' | 'paused'
        rec_count: number
        priority_summary: string
        started_at: string
        completed_at: string
        dispatch_log: string
        rec_items: {
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
        }[]
      }[]
      // Deployer outcome for chains that chained into the deployer
      // (currently only `seo` dispatch_kind). Empty object when this
      // chain didn't run a deploy stage (catalog-audit, h2h, article-
      // author all skip deploy because they're DB-only).
      deploy?: {
        status?: string        // 'success' | 'failure' | 'dry-run' | ''
        tag?: string           // e.g. '20260506-0326'
        image?: string         // e.g. 'nscappsacr.azurecr.io/specpicks'
        site?: string
        started_at?: string
        ended_at?: string
        test_rc?: number | null
        build_rc?: number | null
        push_rc?: number | null
        deploy_rc?: number | null
        smoke_ok?: boolean | null
        failed_stage?: string  // 'test' | 'build' | 'push' | 'deploy' | 'smoke' | ''
        stderr_tail?: string   // truncated to ~400 chars; full text on /api/agents/<src>/runs/<ts>/
      }
    }[]
  }>(`/api/implementer/batches?limit=${limit}`),

  // Lifetime aggregate counts — independent of the windowed /batches
  // endpoint. Used for the top-of-page stats cards so they don't
  // shrink when older chains roll off the page.
  implementerLifetimeStats: () =>
    http<{
      shipped: number
      implemented: number
      deferred: number
      pending: number
      total: number
      by_agent: Record<string, {
        shipped: number; implemented: number; deferred: number;
        pending: number; total: number
      }>
    }>(`/api/implementer/lifetime-stats`),

  // Lifetime per-category rec list — backs the queue page's tab
  // drill-downs. Returns the latest rec snapshot for every rec in
  // the requested category (shipped|implemented|deferred|pending),
  // capped at 200/bucket. Same cache as lifetime-stats so it's
  // always a hit after the first cold compute.
  implementerRecsByCategory: (category: 'shipped' | 'implemented' | 'deferred' | 'pending', limit = 100) =>
    http<{
      category: string
      count: number
      capped_at: number
      recs: Array<{
        rec_id: string
        title: string
        kind: string
        category: string
        agent_id: string
        run_ts: string
        run_dir_basename: string
        shipped: boolean
        implemented: boolean
        deferred: boolean
        commit_sha?: string
        description?: string
        rationale?: string
        severity?: string
        tier?: string
        confidence?: number
        expected_impact?: string
        implementation_outline?: { approach?: string }
        migration_template?: { sql?: string; sql_with_ids?: string; table?: string; action?: string }
        ref_ids?: (string | number)[]
        evidence?: unknown
        check_id?: string
        affected_url?: string
        affected_urls?: string[]
        page_url?: string
        page_path?: string
        url?: string
        files?: string[]
        files_changed?: string[]
        target_files?: string[]
        implemented_at?: string
        implemented_via?: string
        shipped_at?: string
        shipped_tag?: string
        shipped_via?: string
        shipped_verification?: string
        deferred_reason?: string
      }>
    }>(`/api/implementer/recs-by-category?category=${category}&limit=${limit}`),

  getRecVerificationScript: (runDirBasename: string, recId: string) =>
    http<{
      rec_id: string
      rec_type?: string
      generated_at: string
      generated_by: string
      explanation: string
      script_js: string
    }>(`/api/implementer/batches/${encodeURIComponent(runDirBasename)}/rec/${encodeURIComponent(recId)}/verification`),
  proxyFetch: (url: string, opts?: { method?: string; timeoutS?: number; maxBytes?: number }) =>
    http<{
      ok: boolean
      url: string
      status: number
      headers: Record<string, string>
      body: string
      truncated: boolean
      error?: string
    }>(`/api/implementer/proxy/fetch`, {
      method: 'POST',
      body: JSON.stringify({
        url,
        method: opts?.method ?? 'GET',
        timeout_s: opts?.timeoutS ?? 12,
        max_bytes: opts?.maxBytes ?? 200000,
      }),
    }),
  getBatchRecDetail: (runDirBasename: string, recId: string) =>
    http<{
      rec_id: string
      rec: Record<string, unknown>
      summary_md: string
      summary_key?: string
      source_agent?: string
      source_run_ts?: string
      rec_context?: {
        rec_id: string
        kind: string
        summary: string
        fields: Record<string, unknown>
        attachments: string[]
        agent_id: string
        run_ts: string
      } | null
    }>(`/api/implementer/batches/${encodeURIComponent(runDirBasename)}/rec/${encodeURIComponent(recId)}`),
  recContextAttachmentUrl: (runDirBasename: string, recId: string, name: string) =>
    `${API_BASE}/api/implementer/batches/${encodeURIComponent(runDirBasename)}/rec/${encodeURIComponent(recId)}/attachment/${encodeURIComponent(name)}`,

  // dependencies / graph
  dependencyGraph:   (includeBlueprints = false) => http<{
    nodes: { id: string; name: string; category: string; enabled: boolean; is_blueprint: boolean; blueprint?: string; owner: string; cron: string }[]
    edges: { from: string; to: string; kind: string; description: string; default: boolean }[]
    kinds: { id: string; label: string; style: string }[]
  }>(`/api/agents/dependencies?include_blueprints=${includeBlueprints}`),
  patchDependencies: (id: string, depends_on: { agent_id: string; kind: string; description?: string }[]) =>
    http<{ ok: boolean; agent_id: string; depends_on: unknown[] }>(`/api/agents/${encodeURIComponent(id)}/dependencies`, { method: 'PATCH', body: JSON.stringify({ depends_on }) }),
  getGraphLayout:    (userId: string) => http<{ positions: Record<string, { x: number; y: number }>; viewport: Record<string, number> }>(`/api/agents/dependencies/layout/${encodeURIComponent(userId)}`),
  putGraphLayout:    (userId: string, layout: { positions: Record<string, { x: number; y: number }>; viewport: Record<string, number> }) =>
    http<{ ok: boolean }>(`/api/agents/dependencies/layout/${encodeURIComponent(userId)}`, { method: 'PUT', body: JSON.stringify(layout) }),

  // Knowledge buckets — accumulated cross-run findings
  listKnowledgeBuckets: (id: string) =>
    http<{ agent_id: string; buckets: Array<{
      bucket: string; label: string; storage_key: string;
      items_field: string; title_field: string; id_field: string;
      is_legacy: boolean; item_count: number;
      states: Record<string, number>; updated_at?: string;
    }> }>(`/api/agents/${encodeURIComponent(id)}/knowledge`),
  getKnowledgeBucket: (id: string, bucket: string,
                       state: string = 'all', limit: number = 500) =>
    http<{ agent_id: string; bucket: string; label: string;
           title_field: string; id_field: string;
           item_count_total: number; item_count_returned: number;
           states: Record<string, number>; updated_at?: string;
           items: Array<Record<string, unknown>>;
    }>(`/api/agents/${encodeURIComponent(id)}/knowledge/${encodeURIComponent(bucket)}?state=${encodeURIComponent(state)}&limit=${limit}`),
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
