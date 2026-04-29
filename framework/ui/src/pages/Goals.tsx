// Top-level Goals page — fleet-wide view of every agent's goals + trends.
// Two layouts:
//   /goals             — list of agents with goals; click an agent to drill in
//   /goals/<agentId>   — full per-agent goal detail (sparklines + annotations)
//
// Per-run goal-progress.json is tracked by every agent's analyzer step, so
// this page is purely a viewer — no writes happen here.

import { useEffect, useMemo, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { api } from '../api/client'

type TimeseriesGoal = {
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
}
type TimeseriesAnnotation = { ts: string; rec_id: string; title: string; goal_id: string; kind: 'shipped' | 'implemented' }

export default function Goals() {
  const { agentId } = useParams<{ agentId?: string }>()
  if (agentId) {
    return <GoalsAgentDetail agentId={agentId} />
  }
  return <GoalsFleetList />
}

// ── Fleet view: every agent that has goals, with summary trend ──────────

type AgentGoalsSummary = {
  agent_id: string
  agent_name: string
  category: string
  goal_count: number
  goals: TimeseriesGoal[]
  annotations_count: number
  active_count: number
  improving_count: number
  worsening_count: number
  flat_count: number
  recs_shipped_count: number
}

function GoalsFleetList() {
  const [summaries, setSummaries] = useState<AgentGoalsSummary[] | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const agents = await api.listAgents()
        const out: AgentGoalsSummary[] = []
        // Fetch goal time-series in parallel for every agent
        const results = await Promise.allSettled(
          agents.map(a => api.goalsTimeseries(a.id, 30).then(d => ({ a, d })))
        )
        for (const r of results) {
          if (r.status !== 'fulfilled') continue
          const { a, d } = r.value
          if (!d.goals || d.goals.length === 0) continue
          let improving = 0, worsening = 0, flat = 0
          for (const g of d.goals) {
            const pts = (g.points || []).filter(p => p.current != null)
            if (pts.length < 2) { flat++; continue }
            const first = pts[0].current as number
            const last = pts[pts.length - 1].current as number
            if (first === last) { flat++; continue }
            const desiredDecrease = g.baseline != null && g.target != null && g.baseline > g.target
            const isImproving = desiredDecrease ? (last < first) : (last > first)
            if (isImproving) improving++; else worsening++
          }
          out.push({
            agent_id: a.id,
            agent_name: a.name || a.id,
            category: a.category || '',
            goal_count: d.goal_count,
            goals: d.goals,
            annotations_count: (d.annotations || []).length,
            active_count: d.goals.length,
            improving_count: improving,
            worsening_count: worsening,
            flat_count: flat,
            recs_shipped_count: (d.annotations || []).filter(x => x.kind === 'shipped').length,
          })
        }
        if (!cancelled) {
          out.sort((a, b) => b.goal_count - a.goal_count || a.agent_name.localeCompare(b.agent_name))
          setSummaries(out)
        }
      } catch (e: unknown) {
        if (!cancelled) setError(String((e as Error)?.message || e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [])

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl sm:text-2xl font-semibold text-ink-900 tracking-tight">🎯 Goals</h1>
        <p className="text-xs sm:text-sm text-ink-500 mt-0.5">
          Every agent's declared goals + the metric trend over recent runs. Each run records the
          current value of every metric, so you can see the actual movement caused by shipped recs.
        </p>
      </header>

      {error && (
        <div className="px-4 py-2.5 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {loading && <div className="text-ink-500 italic py-8 text-center">Loading goals across the fleet…</div>}

      {!loading && summaries && summaries.length === 0 && (
        <div className="card-surface p-8 text-center">
          <div className="text-4xl mb-2">🎯</div>
          <h2 className="text-lg font-semibold text-ink-900">No agents have goals declared yet.</h2>
          <p className="text-sm text-ink-500 mt-1">
            Agents declare goals during their analysis step. Once an agent has run with goals
            (and produced a <code className="text-xs bg-surface-subtle px-1.5 py-0.5 rounded">goal-progress.json</code>),
            it will appear here.
          </p>
        </div>
      )}

      {!loading && summaries && summaries.length > 0 && (
        <div className="space-y-4">
          {/* Top-line fleet stats */}
          <FleetStats summaries={summaries} />

          {/* Per-agent cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {summaries.map(s => <AgentSummaryCard key={s.agent_id} s={s} />)}
          </div>
        </div>
      )}
    </div>
  )
}

function FleetStats({ summaries }: { summaries: AgentGoalsSummary[] }) {
  const totals = useMemo(() => {
    let goals = 0, improving = 0, worsening = 0, flat = 0, shipped = 0
    for (const s of summaries) {
      goals += s.goal_count
      improving += s.improving_count
      worsening += s.worsening_count
      flat += s.flat_count
      shipped += s.recs_shipped_count
    }
    return { agents: summaries.length, goals, improving, worsening, flat, shipped }
  }, [summaries])
  return (
    <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
      {[
        { label: 'Agents w/ goals', value: totals.agents, color: 'text-ink-700' },
        { label: 'Total goals',     value: totals.goals,    color: 'text-ink-700' },
        { label: 'Improving',       value: totals.improving, color: 'text-emerald-600' },
        { label: 'Worsening',       value: totals.worsening, color: 'text-amber-600' },
        { label: 'Recs shipped',    value: totals.shipped,   color: 'text-blue-600' },
      ].map(s => (
        <div key={s.label} className="card-surface p-3 text-center">
          <div className={`text-xl font-bold ${s.color}`}>{s.value}</div>
          <div className="text-[10px] text-ink-500 mt-0.5">{s.label}</div>
        </div>
      ))}
    </div>
  )
}

function AgentSummaryCard({ s }: { s: AgentGoalsSummary }) {
  // Pick the goal with the most movement to show as the headline sparkline
  const featured = useMemo(() => {
    let best: TimeseriesGoal | null = null
    let bestSpan = -Infinity
    for (const g of s.goals) {
      const pts = (g.points || []).filter(p => p.current != null) as { current: number }[]
      if (pts.length < 2) continue
      const span = Math.abs((pts[pts.length - 1].current) - (pts[0].current))
      if (span > bestSpan) { bestSpan = span; best = g }
    }
    return best || s.goals[0]
  }, [s.goals])
  return (
    <Link
      to={`/goals/${encodeURIComponent(s.agent_id)}`}
      className="card-surface p-4 hover:ring-2 hover:ring-accent-300/40 hover:shadow-sm transition-all flex flex-col gap-2"
    >
      <header className="flex items-start justify-between gap-2 min-w-0">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-ink-900 truncate">{s.agent_name}</div>
          <div className="text-[10px] text-ink-500 font-mono">{s.agent_id}</div>
        </div>
        <span className="status-pill text-[10px] bg-surface-subtle text-ink-600">{s.category || '—'}</span>
      </header>
      <div className="flex flex-wrap gap-2 text-[11px]">
        <span className="status-pill bg-ink-100 text-ink-700">{s.goal_count} goal{s.goal_count === 1 ? '' : 's'}</span>
        {s.improving_count > 0 && (
          <span className="status-pill bg-emerald-50 text-emerald-700 ring-emerald-200 ring-1">↗ {s.improving_count} improving</span>
        )}
        {s.worsening_count > 0 && (
          <span className="status-pill bg-amber-50 text-amber-700 ring-amber-200 ring-1">↘ {s.worsening_count} worsening</span>
        )}
        {s.flat_count > 0 && (
          <span className="status-pill bg-surface-subtle text-ink-500">— {s.flat_count} flat</span>
        )}
        {s.recs_shipped_count > 0 && (
          <span className="status-pill bg-blue-50 text-blue-700 ring-blue-200 ring-1">🚀 {s.recs_shipped_count} shipped</span>
        )}
      </div>
      {featured && <MiniSpark goal={featured} />}
    </Link>
  )
}

function MiniSpark({ goal }: { goal: TimeseriesGoal }) {
  const pts = (goal.points || []).filter(p => p.current != null)
  if (pts.length < 2) {
    return <div className="text-[10px] text-ink-500 italic mt-1">Not enough data — needs ≥2 runs.</div>
  }
  const W = 240, H = 36, PAD = 2
  const ys = pts.map(p => p.current as number)
  const target = goal.target ?? null
  const ymin = Math.min(...ys, target ?? Infinity)
  const ymax = Math.max(...ys, target ?? -Infinity)
  const yspan = Math.max(0.01, ymax - ymin)
  const fx = (i: number) => PAD + (i / Math.max(1, pts.length - 1)) * (W - PAD * 2)
  const fy = (v: number) => PAD + (1 - (v - ymin) / yspan) * (H - PAD * 2)
  const path = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${fx(i).toFixed(1)},${fy(p.current as number).toFixed(1)}`).join(' ')
  const first = ys[0], last = ys[ys.length - 1]
  const desiredDecrease = goal.baseline != null && goal.target != null && goal.baseline > goal.target
  const trend = first === last ? 'flat' : (last > first ? 'up' : 'down')
  const isImproving = desiredDecrease ? (trend === 'down') : (trend === 'up')
  const stroke = trend === 'flat' ? '#94a3b8' : (isImproving ? '#10b981' : '#f59e0b')
  return (
    <div className="mt-1">
      <div className="flex items-center justify-between gap-2 text-[10px] text-ink-500 mb-0.5">
        <span className="truncate min-w-0" title={goal.description}>{goal.description || goal.goal_id}</span>
        <span className="font-mono whitespace-nowrap" style={{ color: stroke }}>
          {first.toFixed(2)} → {last.toFixed(2)}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-8 block" preserveAspectRatio="none">
        {target != null && Number.isFinite(target) && (
          <line x1={PAD} x2={W - PAD} y1={fy(target)} y2={fy(target)}
            stroke="#10b981" strokeWidth={1} strokeDasharray="3 2" opacity={0.4} />
        )}
        <path d={path} fill="none" stroke={stroke} strokeWidth={1.5} />
      </svg>
    </div>
  )
}

// ── Per-agent detail view ─────────────────────────────────────────────────

function GoalsAgentDetail({ agentId }: { agentId: string }) {
  const navigate = useNavigate()
  const [goals, setGoals] = useState<TimeseriesGoal[] | null>(null)
  const [annotations, setAnnotations] = useState<TimeseriesAnnotation[]>([])
  const [runsScanned, setRunsScanned] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.goalsTimeseries(agentId, 90)
      .then(d => {
        if (cancelled) return
        setGoals(d.goals || [])
        setAnnotations(d.annotations || [])
        setRunsScanned(d.runs_scanned || 0)
      })
      .catch(e => { if (!cancelled) setError(String((e as Error)?.message || e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [agentId])

  return (
    <div className="space-y-5">
      <header className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <button onClick={() => navigate('/goals')} className="text-sm text-accent-600 hover:text-accent-800 mb-1">
            ← All agents
          </button>
          <h1 className="text-xl sm:text-2xl font-semibold text-ink-900 tracking-tight">
            🎯 {agentId} — goals
          </h1>
          <p className="text-xs sm:text-sm text-ink-500 mt-0.5">
            {runsScanned} run{runsScanned === 1 ? '' : 's'} scanned · {goals?.length ?? 0} goals · {annotations.length} rec annotation{annotations.length === 1 ? '' : 's'}
          </p>
        </div>
        <Link to={`/agents/${encodeURIComponent(agentId)}`} className="btn-secondary !text-xs">
          → agent detail
        </Link>
      </header>

      {error && (
        <div className="px-4 py-2.5 bg-status-failure-bg border border-status-failure-glow/40 rounded-lg text-sm text-status-failure-fg">
          {error}
        </div>
      )}

      {loading && <div className="text-ink-500 italic py-8 text-center">Loading goal time-series…</div>}

      {!loading && goals && goals.length === 0 && (
        <div className="card-surface p-8 text-center">
          <div className="text-4xl mb-2">🎯</div>
          <h2 className="text-lg font-semibold text-ink-900">No goal data yet.</h2>
          <p className="text-sm text-ink-500 mt-1">
            This agent hasn't produced a <code>goal-progress.json</code> yet. The first run after
            goals are declared starts the time-series.
          </p>
        </div>
      )}

      {!loading && goals && goals.length > 0 && (
        <div className="space-y-3">
          {goals.map(g => (
            <DetailedGoalRow
              key={g.goal_id}
              goal={g}
              annotations={annotations.filter(a => a.goal_id === g.goal_id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function DetailedGoalRow({
  goal, annotations,
}: { goal: TimeseriesGoal; annotations: TimeseriesAnnotation[] }) {
  const [expanded, setExpanded] = useState(false)
  const points = (goal.points || []).filter(p => p.current != null && Number.isFinite(p.current as number))
  if (points.length === 0) {
    return (
      <div className="card-surface p-4 text-sm">
        <div className="font-semibold text-ink-700">{goal.description || goal.goal_id}</div>
        <div className="text-ink-500 mt-1">No measurements yet.</div>
      </div>
    )
  }
  const W = 800, H = 100, PAD = 6
  const ys = points.map(p => p.current as number)
  const target = goal.target ?? null
  const baseline = goal.baseline ?? null
  const ymin = Math.min(...ys, target ?? Infinity, baseline ?? Infinity)
  const ymax = Math.max(...ys, target ?? -Infinity, baseline ?? -Infinity)
  const yspan = Math.max(0.01, ymax - ymin)
  const fx = (i: number) => PAD + (i / Math.max(1, points.length - 1)) * (W - PAD * 2)
  const fy = (v: number) => PAD + (1 - (v - ymin) / yspan) * (H - PAD * 2)
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${fx(i).toFixed(1)},${fy(p.current as number).toFixed(1)}`).join(' ')
  const annX = (annTs: string): number | null => {
    if (!annTs) return null
    let bestI = -1, bestDelta = Infinity
    const at = Date.parse(annTs)
    for (let i = 0; i < points.length; i++) {
      const pt = Date.parse(points[i].ts)
      const d = Math.abs(pt - at)
      if (d < bestDelta) { bestDelta = d; bestI = i }
    }
    return bestI >= 0 ? fx(bestI) : null
  }
  const first = ys[0], last = ys[ys.length - 1]
  const desiredDecrease = baseline != null && target != null && baseline > target
  const trend = first === last ? 'flat' : (last > first ? 'up' : 'down')
  const isImproving = desiredDecrease ? (trend === 'down') : (trend === 'up')
  const trendColor = trend === 'flat' ? 'text-ink-500' : (isImproving ? 'text-emerald-600' : 'text-amber-600')
  const trendStroke = trend === 'flat' ? '#94a3b8' : (isImproving ? '#10b981' : '#f59e0b')
  const trendLabel = trend === 'flat' ? '— no movement' : (isImproving ? '↗ improving' : '↘ worsening')
  const movementPct = (() => {
    if (target == null || target === 0) return null
    if (desiredDecrease) {
      // measure progress from baseline → target
      if (baseline == null || baseline === target) return null
      const total = baseline - target
      const closed = baseline - last
      return Math.max(0, Math.min(100, (closed / total) * 100))
    }
    if (baseline == null || baseline === target) return null
    const total = target - baseline
    const closed = last - baseline
    return Math.max(0, Math.min(100, (closed / total) * 100))
  })()

  return (
    <div className="card-surface p-4">
      <header className="flex items-baseline justify-between gap-3 flex-wrap mb-2">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-ink-900">{goal.description || goal.goal_id}</div>
          <div className="text-[10px] text-ink-500 font-mono mt-0.5">{goal.target_metric}</div>
          {goal.rationale && (
            <div className="text-[11px] text-ink-500 mt-1 italic">"{goal.rationale}"</div>
          )}
        </div>
        <div className="text-right text-xs whitespace-nowrap">
          <div className={`font-mono font-semibold ${trendColor}`}>
            {first.toFixed(2)} → {last.toFixed(2)}
          </div>
          <div className={`text-[10px] ${trendColor}`}>{trendLabel}</div>
          {target != null && (
            <div className="text-[10px] text-ink-500">
              target: <span className="font-mono">{target}</span>
              {baseline != null && <> · baseline: <span className="font-mono">{baseline}</span></>}
            </div>
          )}
          {movementPct != null && (
            <div className="text-[10px] text-ink-500">
              {movementPct.toFixed(0)}% of goal closed
            </div>
          )}
        </div>
      </header>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-20 block" preserveAspectRatio="none">
        {target != null && Number.isFinite(target) && (
          <>
            <line x1={PAD} x2={W - PAD} y1={fy(target)} y2={fy(target)}
              stroke="#10b981" strokeWidth={1} strokeDasharray="4 3" opacity={0.5} />
            <text x={W - PAD - 2} y={fy(target) - 3} textAnchor="end"
              fontSize="9" fill="#059669" opacity={0.7}>target {target}</text>
          </>
        )}
        {baseline != null && Number.isFinite(baseline) && (
          <>
            <line x1={PAD} x2={W - PAD} y1={fy(baseline)} y2={fy(baseline)}
              stroke="#94a3b8" strokeWidth={1} strokeDasharray="2 2" opacity={0.4} />
            <text x={PAD + 2} y={fy(baseline) - 3} textAnchor="start"
              fontSize="9" fill="#64748b" opacity={0.7}>baseline {baseline}</text>
          </>
        )}
        <path d={path} fill="none" stroke={trendStroke} strokeWidth={1.8} />
        {points.map((p, i) => (
          <circle key={i} cx={fx(i)} cy={fy(p.current as number)} r={2.2} fill={trendStroke}>
            <title>{`${p.ts.slice(0, 19)} → ${p.current}`}</title>
          </circle>
        ))}
        {annotations.map((a, i) => {
          const x = annX(a.ts)
          if (x == null) return null
          const color = a.kind === 'shipped' ? '#2563eb' : '#10b981'
          return (
            <g key={`${a.rec_id}-${i}`}>
              <line x1={x} x2={x} y1={PAD} y2={H - PAD} stroke={color} strokeWidth={0.8} opacity={0.5} />
              <polygon points={`${x - 3.5},${PAD + 1} ${x + 3.5},${PAD + 1} ${x},${PAD + 6}`}
                fill={color} opacity={0.85}>
                <title>{`${a.kind} · ${a.rec_id}: ${a.title}`}</title>
              </polygon>
            </g>
          )
        })}
      </svg>
      <div className="flex justify-between text-[10px] text-ink-400 mt-1">
        <span>{points[0].ts.slice(0, 19).replace('T', ' ')}</span>
        <span>{points.length} measurements{annotations.length > 0 ? ` · ${annotations.length} rec${annotations.length === 1 ? '' : 's'} shipped` : ''}</span>
        <span>{points[points.length - 1].ts.slice(0, 19).replace('T', ' ')}</span>
      </div>
      <div className="mt-3 border-t border-surface-divider pt-3">
        <button onClick={() => setExpanded(!expanded)} className="text-xs text-accent-600 hover:text-accent-800">
          {expanded ? '▾ hide measurements + annotations' : '▸ show measurements + annotations'}
        </button>
        {expanded && (
          <div className="mt-2 grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div>
              <h4 className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">
                Measurements ({points.length})
              </h4>
              <ul className="text-[11px] divide-y divide-surface-divider rounded border border-surface-divider overflow-hidden max-h-72 overflow-y-auto">
                {[...points].reverse().map((p, i) => (
                  <li key={i} className="px-2 py-1 flex justify-between gap-2 hover:bg-surface-subtle">
                    <span className="text-ink-500 font-mono">{p.ts.slice(0, 19)}</span>
                    <span className="text-ink-800 font-mono">{p.current?.toFixed(2)}</span>
                    {p.status && <span className="text-ink-500 text-[10px]">{p.status}</span>}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h4 className="text-[10px] uppercase tracking-wide text-ink-500 font-semibold mb-1">
                Recs targeting this goal ({annotations.length})
              </h4>
              {annotations.length === 0 ? (
                <p className="text-[11px] text-ink-500 italic">None yet — recs that target this goal will appear here as they're shipped.</p>
              ) : (
                <ul className="text-[11px] divide-y divide-surface-divider rounded border border-surface-divider overflow-hidden max-h-72 overflow-y-auto">
                  {annotations.map((a, i) => (
                    <li key={i} className="px-2 py-1 flex justify-between gap-2 hover:bg-surface-subtle">
                      <span className="font-mono text-ink-500">{a.rec_id}</span>
                      <span className="text-ink-800 truncate flex-1" title={a.title}>{a.title}</span>
                      <span className={a.kind === 'shipped' ? 'text-blue-600' : 'text-emerald-600'}>
                        {a.kind === 'shipped' ? '🚀' : '✅'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
