import type { AgentState } from '../api/types'

const PILL: Record<string, { fg: string; bg: string; ring: string; label: string }> = {
  running:   { fg: 'text-status-running-fg',  bg: 'bg-status-running-bg',  ring: 'ring-status-running-glow/30', label: '● running' },
  starting:  { fg: 'text-status-starting-fg', bg: 'bg-status-starting-bg', ring: 'ring-status-starting-glow/30', label: '▶ starting' },
  failure:   { fg: 'text-status-failure-fg',  bg: 'bg-status-failure-bg',  ring: 'ring-status-failure-glow/30', label: '✕ failure' },
  blocked:   { fg: 'text-status-blocked-fg',  bg: 'bg-status-blocked-bg',  ring: 'ring-status-blocked-glow/30', label: '⏸ blocked' },
  success:   { fg: 'text-status-success-fg',  bg: 'bg-status-success-bg',  ring: 'ring-status-success-glow/30', label: '✓ success' },
  idle:      { fg: 'text-status-idle-fg',     bg: 'bg-status-idle-bg',     ring: 'ring-status-idle-glow/30',    label: '○ idle' },
  cancelled: { fg: 'text-status-idle-fg',     bg: 'bg-status-idle-bg',     ring: 'ring-status-idle-glow/30',    label: '○ cancelled' },
}

export default function StatusBadge({ state, pulsing = false }: { state: AgentState | string; pulsing?: boolean }) {
  const s = String(state ?? '')
  const p = PILL[s] ?? { fg: 'text-ink-500', bg: 'bg-surface-subtle', ring: 'ring-surface-divider', label: s || '· no runs' }
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ring-1 ring-inset ${p.fg} ${p.bg} ${p.ring} ${pulsing ? 'animate-pulse' : ''}`}
    >
      {p.label}
    </span>
  )
}
