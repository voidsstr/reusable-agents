// Agent card with state-driven glow animation. Mirrors the pattern from
// application-research/frontend/src/pages/MarketResearch.tsx but parameterized
// per state via a CSS variable.

import { Link } from 'react-router-dom'
import type { AgentSummary, AgentLiveStatus } from '../api/types'
import StatusBadge from './StatusBadge'

const STATE_RGB: Record<string, string> = {
  running:  '56 189 248',
  starting: '168 85 247',
  failure:  '239 68 68',
  blocked:  '245 158 11',
  success:  '16 185 129',
  idle:     '100 116 139',
  cancelled:'100 116 139',
  '':       '100 116 139',
}

const CATEGORY_EMOJI: Record<string, string> = {
  seo: '🎯', research: '🔬', fleet: '🖥', personal: '📅', ops: '⚙️', misc: '📦',
}

interface Props {
  agent: AgentSummary
  status?: AgentLiveStatus | null   // optional — pushes from WebSocket take priority
  onTrigger?: (id: string) => void
  onToggleEnabled?: (a: AgentSummary) => void
}

export default function AgentCard({ agent, status, onTrigger, onToggleEnabled }: Props) {
  // Effective state: prefer the live status (WebSocket-pushed) over the
  // denormalized last_run_status from registry.
  const liveState = status?.state ?? agent.last_run_status ?? ''
  const isActive = liveState === 'running' || liveState === 'starting'
  const rgb = STATE_RGB[liveState] ?? STATE_RGB['']
  const emoji = CATEGORY_EMOJI[agent.category] ?? CATEGORY_EMOJI.misc
  const liveMessage = status?.message ?? agent.cron_expr ? `cron: ${agent.cron_expr}` : ''

  return (
    <Link
      to={`/agents/${encodeURIComponent(agent.id)}`}
      className={`agent-card-glow ${isActive ? 'is-active' : ''} block bg-ink-800 hover:bg-ink-700/70 p-4 ${agent.enabled ? '' : 'opacity-60'}`}
      style={{ ['--glow-color' as string]: rgb }}
    >
      <div className="flex justify-between items-start gap-2 mb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span>{emoji}</span>
            <h3 className="font-semibold text-ink-100 truncate">{agent.name}</h3>
          </div>
          <div className="text-[10px] text-ink-500 font-mono truncate">{agent.id}</div>
        </div>
        <StatusBadge state={liveState} pulsing={isActive} />
      </div>

      {agent.description && (
        <div className="text-xs text-ink-300 line-clamp-2 mb-2">{agent.description}</div>
      )}

      <div className="text-[11px] text-ink-400 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 font-mono">
        <span className="text-ink-500">cron:</span>
        <span className="truncate">{agent.cron_expr || '(manual)'} <span className="text-ink-600">{agent.timezone}</span></span>
        <span className="text-ink-500">type:</span>
        <span>{agent.task_type}</span>
        {agent.last_run_at && (
          <>
            <span className="text-ink-500">last:</span>
            <span className="truncate">{agent.last_run_at}</span>
          </>
        )}
      </div>

      {status?.current_action && (
        <div className="mt-2 text-[11px] text-ink-300 italic line-clamp-1">
          ▸ {status.current_action} — {status.message}
        </div>
      )}

      {status && status.progress > 0 && status.progress < 1 && (
        <div className="mt-2 h-1 bg-ink-700 rounded-full overflow-hidden">
          <div
            className="h-full transition-all"
            style={{ width: `${(status.progress * 100).toFixed(0)}%`, backgroundColor: `rgb(${rgb})` }}
          />
        </div>
      )}

      <div
        className="flex gap-1 mt-3 pt-2 border-t border-ink-700/50"
        onClick={(e) => e.preventDefault()}
      >
        <button
          onClick={(e) => { e.stopPropagation(); onTrigger?.(agent.id) }}
          className="text-[11px] px-2 py-1 bg-ink-700 hover:bg-glow-running/20 hover:text-glow-running rounded transition-colors"
          disabled={isActive}
          title={isActive ? 'A run is already in progress' : 'Trigger a run now'}
        >
          ▶ run
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onToggleEnabled?.(agent) }}
          className="text-[11px] px-2 py-1 bg-ink-700 hover:bg-ink-600 rounded transition-colors"
        >
          {agent.enabled ? '⏸ pause' : '▶ enable'}
        </button>
      </div>
    </Link>
  )
}
