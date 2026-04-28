// Mirror of the FastAPI route response shapes.

export type AgentState =
  | 'idle' | 'starting' | 'running' | 'success' | 'failure' | 'blocked' | 'cancelled' | 'unknown' | ''

export type RunnableMode = 'cron' | 'manual' | 'chained'

export interface ConfirmationFlow {
  enabled?: boolean
  kind?: 'email-recommendations' | 'per-action' | 'preview-mode' | 'upstream-gated' | 'none' | string
  description?: string
  owner_email?: string
}

export interface AgentSummary {
  id: string
  name: string
  description: string
  category: string
  task_type: string
  cron_expr: string
  timezone: string
  enabled: boolean
  owner: string
  last_run_status: AgentState
  last_run_at: string | null
  next_run_at: string | null
  runnable_modes: RunnableMode[]
  confirmation_flow: ConfirmationFlow
  application: string
  ai_provider?: string
  ai_kind?: string
  ai_model?: string
  ai_uses_claude?: boolean
}

export interface AgentDetail extends AgentSummary {
  repo_dir: string
  runbook_path: string
  skill_path: string
  entry_command: string
  capabilities: string[]
  capabilities_detail: CapabilityDetail[]
  metadata: Record<string, unknown>
  depends_on: { agent_id: string; kind: string; description?: string }[]
  runbook_body: string | null
  skill_body: string | null
  readme_body: string | null
  current_status: AgentLiveStatus | null
  recent_runs: RunSummary[]
}

export interface CapabilityDetail {
  name: string
  description: string
  confirmation_required: boolean
  risk_level: 'low' | 'medium' | 'high' | 'critical'
  affects: string[]
  notes: string
}

export interface AgentLiveStatus {
  schema_version: string
  agent_id: string
  state: AgentState
  message: string
  progress: number
  current_action: string
  started_at: string
  updated_at: string
  current_run_ts: string
  iteration_count: number
  internal: Record<string, unknown>
}

export interface RunSummary {
  agent_id: string
  run_ts: string
  status: AgentState
  started_at: string
  ended_at: string | null
  summary: string
  iteration_count: number
  progress: number
}

export interface RunDetail {
  agent_id: string
  run_ts: string
  progress: Record<string, unknown> | null
  decisions: Decision[]
  context_summary_md: string
  recommendations: unknown
  responses: unknown
  deploy: unknown
}

export interface Decision {
  ts: string
  category: string
  message: string
  rec_id?: string
  action?: string
  evidence?: Record<string, unknown>
}

export interface Message {
  schema_version: string
  message_id: string
  from: string
  to: string[]
  kind: string
  subject: string
  body: Record<string, unknown>
  in_reply_to: string
  ts: string
  read_by: Record<string, string>
}

export interface ConfirmationRecord {
  confirmation_id: string
  agent_id: string
  method_name: string
  reason: string
  state: 'pending' | 'approved' | 'rejected' | 'expired'
  requested_at: string
  request_id: string
  resolved_at: string
  approved_by: string
  notes: string
}

export interface GoalMetric {
  name?: string
  current?: number
  target?: number
  direction?: 'increase' | 'decrease'
  unit?: string
  horizon_weeks?: number
}
export interface GoalProgressEntry {
  ts: string
  value: number
  run_ts?: string
  note?: string
}
export interface Goal {
  id: string
  title: string
  description?: string
  metric?: GoalMetric
  status?: 'active' | 'accomplished' | 'abandoned' | 'blocked'
  created_at?: string
  accomplished_at?: string | null
  progress_history?: GoalProgressEntry[]
  directives?: string[]
  owner_email?: string
}

export interface FrameworkEvent {
  ts: string
  agent_id?: string
  run_ts?: string
  state?: string
  kind?: string
  action?: string
  message?: string
  current_action?: string
  changed_fields?: string[]
}

export interface ChangelogEntry {
  ts: string
  agent_id: string
  kind: string
  message: string
  release_id?: string
  commit_sha?: string
  files?: string[]
  extra?: Record<string, unknown>
}

export interface DispatchEntry {
  id: string
  site: string
  run_ts: string
  log_filename: string
  started_at: string
  size_bytes: number
  status: 'running' | 'completed' | 'not_found'
  rec_ids: string[]
  rec_count: number
  commit_sha: string
  done: boolean
  tail?: string
  content?: string
}
