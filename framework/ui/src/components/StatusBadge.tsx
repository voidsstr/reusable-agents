import type { AgentState } from '../api/types'

const STATE_RGB: Record<string, string> = {
  running:  '56 189 248',
  success:  '16 185 129',
  failure:  '239 68 68',
  blocked:  '245 158 11',
  starting: '168 85 247',
  cancelled:'148 163 184',
  idle:     '148 163 184',
  '':       '148 163 184',
  unknown:  '148 163 184',
}

const LABEL: Record<string, string> = {
  running:   '● running',
  success:   '✓ success',
  failure:   '✕ failure',
  blocked:   '⏸ blocked',
  starting:  '▶ starting',
  cancelled: '○ cancelled',
  idle:      '○ idle',
  '':        '· no runs',
  unknown:   '· unknown',
}

export default function StatusBadge({ state, pulsing = false }: { state: AgentState | string; pulsing?: boolean }) {
  const rgb = STATE_RGB[state ?? ''] ?? STATE_RGB['']
  const label = LABEL[state ?? ''] ?? state
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded font-mono"
      style={{
        ['--glow-color' as string]: rgb,
        color: `rgb(${rgb})`,
        backgroundColor: `rgb(${rgb} / 0.12)`,
        border: `1px solid rgb(${rgb} / 0.4)`,
        animation: pulsing ? 'glow-pulse 2s ease-in-out infinite' : undefined,
      }}
    >
      {label}
    </span>
  )
}
