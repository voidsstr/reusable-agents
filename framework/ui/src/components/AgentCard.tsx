// Agent card — light-theme, status-driven left stripe + glow on running.
// Pattern inspired by Linear's project cards + Vercel's deployment cards.
//
// Layout:
//   ┌──────────────────────────────────┐
//   │ ⚙ Name                    [pill] │   ← header: emoji + title + status
//   │ <small id>                       │
//   │                                  │
//   │ description (clamp-2)            │
//   │ [badges row]                     │
//   │ ─────                            │
//   │ cron · type · last run           │   ← meta
//   │ ▸ current_action — message       │   ← only when running
//   │ [progress bar]                   │
//   │ [▶ run] [⏸ pause]                │
//   └──────────────────────────────────┘
//      ↑ left edge stripe in status color (auto via .agent-card-glow::before)

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { AgentSummary, AgentLiveStatus } from '../api/types'

const STATE_RGB: Record<string, string> = {
  running:  '59 130 246',   // accent / status-running-glow
  starting: '139 92 246',   // status-starting-glow
  failure:  '239 68 68',
  blocked:  '245 158 11',
  success:  '16 185 129',
  idle:     '148 163 184',
  cancelled:'148 163 184',
  '':       '203 213 225',  // slate-300 fallback
}

const STATE_PILL: Record<string, { fg: string; bg: string; ring: string; label: string }> = {
  running:  { fg: 'text-status-running-fg',  bg: 'bg-status-running-bg',  ring: 'ring-status-running-glow/30', label: 'running' },
  starting: { fg: 'text-status-starting-fg', bg: 'bg-status-starting-bg', ring: 'ring-status-starting-glow/30', label: 'starting' },
  failure:  { fg: 'text-status-failure-fg',  bg: 'bg-status-failure-bg',  ring: 'ring-status-failure-glow/30', label: 'failure' },
  blocked:  { fg: 'text-status-blocked-fg',  bg: 'bg-status-blocked-bg',  ring: 'ring-status-blocked-glow/30', label: 'blocked' },
  success:  { fg: 'text-status-success-fg',  bg: 'bg-status-success-bg',  ring: 'ring-status-success-glow/30', label: 'success' },
  idle:     { fg: 'text-status-idle-fg',     bg: 'bg-status-idle-bg',     ring: 'ring-status-idle-glow/30',    label: 'idle' },
  cancelled:{ fg: 'text-status-idle-fg',     bg: 'bg-status-idle-bg',     ring: 'ring-status-idle-glow/30',    label: 'cancelled' },
}

const CATEGORY_EMOJI: Record<string, string> = {
  seo: '🎯', research: '🔬', fleet: '🖥', personal: '📅', ops: '⚙️', misc: '📦',
}

interface Props {
  agent: AgentSummary
  status?: AgentLiveStatus | null
  onTrigger?: (id: string) => void
  onToggleEnabled?: (a: AgentSummary) => void
}

function formatDuration(sec: number): string {
  if (sec < 0 || !Number.isFinite(sec)) return ''
  if (sec < 60) return `${Math.floor(sec)}s`
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  const mm = m % 60
  return `${h}h ${mm}m`
}

function useTickingNow(active: boolean): number {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

export default function AgentCard({ agent, status, onTrigger, onToggleEnabled }: Props) {
  const liveState = status?.state ?? agent.last_run_status ?? ''
  const isActive = liveState === 'running' || liveState === 'starting'
  const rgb = STATE_RGB[liveState] ?? STATE_RGB['']
  const pill = STATE_PILL[liveState] ?? STATE_PILL.idle
  const emoji = CATEGORY_EMOJI[agent.category] ?? CATEGORY_EMOJI.misc
  const now = useTickingNow(isActive)
  const startedMs = status?.started_at ? Date.parse(status.started_at) : NaN
  const runDuration = isActive && Number.isFinite(startedMs)
    ? formatDuration((now - startedMs) / 1000)
    : ''

  return (
    <Link
      to={`/agents/${encodeURIComponent(agent.id)}`}
      className={`agent-card-glow ${isActive ? 'is-active' : ''} relative block p-4 ${agent.enabled ? '' : 'opacity-65'}`}
      style={{ ['--glow-color' as string]: rgb }}
    >
      {/* Header: emoji · title · status pill */}
      <div className="flex justify-between items-start gap-2 mb-2">
        <div className="min-w-0 flex-1 pl-2">
          <div className="flex items-center gap-1.5">
            <span aria-hidden className="text-base">{emoji}</span>
            <h3 className="font-semibold text-ink-900 truncate text-[15px] tracking-tight">{agent.name}</h3>
          </div>
          <div className="text-[10px] text-ink-400 font-mono truncate mt-0.5">{agent.id}</div>
        </div>
        <div className="flex flex-col items-end gap-0.5">
          <span className={`status-pill ${pill.fg} ${pill.bg} ${pill.ring}`}>
            <span className="status-dot" style={{ ['--glow-color' as string]: rgb }} />
            {pill.label}
          </span>
          {runDuration && (
            <span className="text-[10px] font-mono text-status-running-fg" title={`Running since ${status?.started_at}`}>
              ⏱ {runDuration}
            </span>
          )}
        </div>
      </div>

      {/* Description */}
      {agent.description && (
        <div className="text-[13px] text-ink-600 line-clamp-2 mb-3 pl-2 leading-snug">
          {agent.description}
        </div>
      )}

      {/* Badges */}
      <div className="pl-2"><AgentBadges agent={agent} /></div>
      <div className="pl-2"><AgentAIBadge agent={agent} /></div>

      {/* Meta */}
      <div className="text-[11px] text-ink-500 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 font-mono pl-2 mt-2">
        <span className="text-ink-400">cron</span>
        <span className="truncate">{agent.cron_expr || '(manual)'} <span className="text-ink-300">{agent.timezone}</span></span>
        <span className="text-ink-400">type</span>
        <span>{agent.task_type}</span>
        {agent.last_run_at && (
          <>
            <span className="text-ink-400">last</span>
            <span className="truncate">{agent.last_run_at}</span>
          </>
        )}
      </div>

      {/* Live action — only when status is being pushed */}
      {status?.current_action && (
        <div className="mt-2.5 pl-2 text-[11px] text-ink-700 italic line-clamp-1 flex items-center gap-1.5">
          <span className="text-accent-600 not-italic">▸</span>
          <span className="truncate">{status.current_action} — {status.message}</span>
        </div>
      )}

      {/* Progress bar */}
      {status && status.progress > 0 && status.progress < 1 && (
        <div className="mt-2 ml-2 h-1 bg-surface-divider rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all"
            style={{ width: `${(status.progress * 100).toFixed(0)}%`, backgroundColor: `rgb(${rgb})` }}
          />
        </div>
      )}

      {/* Actions */}
      <div
        className="flex gap-1.5 mt-3 pt-2.5 pl-2 border-t border-surface-divider"
        onClick={(e) => e.preventDefault()}
      >
        {(() => {
          const manualOk = !agent.runnable_modes || agent.runnable_modes.includes('manual')
          if (!manualOk) {
            return (
              <button
                disabled
                className="text-[11px] px-2.5 py-1 bg-surface-subtle text-ink-400 rounded-md cursor-not-allowed"
                title={`Queue-driven agent (runnable_modes=${JSON.stringify(agent.runnable_modes)}). Triggered by upstream agent.`}
              >
                🔒 queue-driven
              </button>
            )
          }
          return (
            <button
              onClick={(e) => { e.stopPropagation(); onTrigger?.(agent.id) }}
              className="btn-primary !py-1 !px-2.5 !text-[11px]"
              disabled={isActive}
              title={isActive ? 'A run is already in progress' : 'Trigger a run now'}
            >
              ▶ run
            </button>
          )
        })()}
        <button
          onClick={(e) => { e.stopPropagation(); onToggleEnabled?.(agent) }}
          className="btn-secondary !py-1 !px-2.5 !text-[11px]"
        >
          {agent.enabled ? '⏸ pause' : '▶ enable'}
        </button>
      </div>
    </Link>
  )
}


function AgentAIBadge({ agent }: { agent: AgentSummary }) {
  // Distinguish LLM-using agents from pure script/cron agents — the user
  // wants this at-a-glance: claude usage = bills against Max plan.
  const usesClaude = agent.ai_uses_claude
  const provider = agent.ai_provider || ''
  const kind = agent.ai_kind || ''
  const model = agent.ai_model || ''

  if (usesClaude) {
    return (
      <div className="mb-2">
        <span
          className="inline-flex items-center gap-1.5 text-[10px] px-1.5 py-0.5 rounded-md font-medium bg-gradient-to-r from-amber-50 to-orange-50 text-amber-800 ring-1 ring-amber-200"
          title={`Uses Claude via ${kind}${model ? ' (' + model + ')' : ''}`}
        >
          <span aria-hidden>🧠</span>
          {kind === 'claude-cli' ? 'Claude (Max)' : 'Claude API'}
          {model && (
            <span className="text-amber-600/70 font-normal">{model.replace('claude-', '').replace(/-/g, ' ')}</span>
          )}
        </span>
      </div>
    )
  }
  if (provider) {
    // Other LLM provider (ollama, copilot, openai)
    return (
      <div className="mb-2">
        <span
          className="inline-flex items-center gap-1.5 text-[10px] px-1.5 py-0.5 rounded-md font-medium bg-status-starting-bg text-status-starting-fg ring-1 ring-status-starting-glow/30"
          title={`Uses ${kind}${model ? ' / ' + model : ''}`}
        >
          <span aria-hidden>🤖</span>
          {kind || provider}
          {model && <span className="opacity-70 font-normal">{model}</span>}
        </span>
      </div>
    )
  }
  // No LLM — pure script / cron job
  return (
    <div className="mb-2">
      <span
        className="inline-flex items-center gap-1.5 text-[10px] px-1.5 py-0.5 rounded-md font-medium bg-surface-subtle text-ink-500 ring-1 ring-surface-divider"
        title="Pure script / cron job — no LLM"
      >
        <span aria-hidden>⚙</span>
        Script-only
      </span>
    </div>
  )
}


function AgentBadges({ agent }: { agent: AgentSummary }) {
  const cf = agent.confirmation_flow || {}
  const modes = agent.runnable_modes || ['cron', 'manual']
  const queueDriven = !modes.includes('manual')

  const badges: { label: string; bg: string; fg: string; title: string }[] = []

  if (cf.kind === 'email-recommendations') {
    badges.push({
      label: '✉ confirms via email',
      bg: 'bg-status-success-bg', fg: 'text-status-success-fg',
      title: cf.description || 'Recs are emailed; user reply gates implementation.',
    })
  } else if (cf.kind === 'upstream-gated') {
    badges.push({
      label: '⤴ upstream-gated',
      bg: 'bg-status-starting-bg', fg: 'text-status-starting-fg',
      title: cf.description || 'Confirmation happens at an upstream agent.',
    })
  } else if (cf.kind === 'per-action') {
    badges.push({
      label: '🛡 per-action',
      bg: 'bg-status-blocked-bg', fg: 'text-status-blocked-fg',
      title: cf.description || 'Each dangerous action requires explicit confirmation.',
    })
  } else if (cf.enabled) {
    badges.push({
      label: '🛡 confirmation gate',
      bg: 'bg-status-success-bg', fg: 'text-status-success-fg',
      title: cf.description || 'Has a confirmation gate before acting.',
    })
  }

  if (queueDriven) {
    badges.push({
      label: '🔒 queue-driven',
      bg: 'bg-surface-subtle', fg: 'text-ink-600',
      title: `runnable_modes=${JSON.stringify(modes)} — only triggered by upstream dispatch.`,
    })
  } else if (modes.includes('cron') && modes.includes('manual') && !agent.cron_expr) {
    badges.push({
      label: '✋ manual',
      bg: 'bg-surface-subtle', fg: 'text-ink-500',
      title: 'No cron schedule — runs only on manual trigger.',
    })
  }

  if (badges.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1 mb-2">
      {badges.map((b, i) => (
        <span
          key={i}
          title={b.title}
          className={`text-[10px] px-1.5 py-0.5 rounded-md font-medium ${b.bg} ${b.fg}`}
        >
          {b.label}
        </span>
      ))}
    </div>
  )
}
