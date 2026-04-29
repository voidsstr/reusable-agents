// n8n-style dependency graph for the agent fleet.
//
// - Nodes = registered agents, color-coded by category
// - Edges = framework defaults + manifest.depends_on overrides
// - Drag to reposition; positions persist to localStorage (per-browser)
//   and optionally to /api/agents/dependencies/layout/<user-id> server-side.
// - Auto-layout via elkjs on first load (or when "Auto layout" is hit)
// - Pan/zoom, MiniMap, Controls, Background grid
// - Click a node to see agent details + outgoing/incoming edges
//
// Built on react-flow (the same library n8n uses).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Panel,
  ReactFlowProvider,
  addEdge,
  applyNodeChanges,
  applyEdgeChanges,
  useReactFlow,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type Connection,
  type ReactFlowInstance,
  type Viewport,
  Handle,
  Position,
  MarkerType,
} from 'reactflow'
import 'reactflow/dist/style.css'
import ELK from 'elkjs/lib/elk.bundled.js'

import { api, openStatusWS } from '../api/client'
import type { AgentLiveStatus } from '../api/types'

const STORAGE_KEY = 'framework-graph-layout:v1'

const CATEGORY_COLORS: Record<string, { bg: string; ring: string; text: string }> = {
  seo:      { bg: '#0c4a6e', ring: '#38bdf8', text: '#e0f2fe' },
  research: { bg: '#3b0764', ring: '#a78bfa', text: '#ede9fe' },
  fleet:    { bg: '#064e3b', ring: '#34d399', text: '#d1fae5' },
  personal: { bg: '#7c2d12', ring: '#fb923c', text: '#fed7aa' },
  ops:      { bg: '#7f1d1d', ring: '#f87171', text: '#fee2e2' },
  misc:     { bg: '#374151', ring: '#9ca3af', text: '#e5e7eb' },
}

const KIND_STYLES: Record<string, { stroke: string; strokeDasharray?: string; animated?: boolean; label?: string }> = {
  triggers:           { stroke: '#38bdf8', animated: true, label: 'triggers' },
  'feeds-run-dir':    { stroke: '#22c55e', label: 'feeds run dir' },
  'sends-email-via':  { stroke: '#f59e0b', strokeDasharray: '6 4', label: 'email→' },
  'polls-replies-for':{ stroke: '#a78bfa', strokeDasharray: '6 4', label: 'polls replies' },
  'routes-replies-to':{ stroke: '#a78bfa', strokeDasharray: '6 4', label: 'routes replies' },
  'dispatches-to':    { stroke: '#ec4899', strokeDasharray: '2 4', animated: true, label: 'auto-dispatch' },
  'config-shared-with':{ stroke: '#94a3b8', strokeDasharray: '1 3', label: 'shares config' },
  'depends-on':       { stroke: '#94a3b8', label: 'depends on' },
}

// ---------- Custom node ----------

type NodeData = {
  name: string; id: string; category: string; enabled: boolean
  cron: string; selected: boolean; live?: AgentLiveStatus | null
}

const STATE_GLOW_RGB: Record<string, string> = {
  running:  '56 189 248',
  starting: '168 85 247',
  failure:  '239 68 68',
  blocked:  '245 158 11',
  success:  '16 185 129',
}

function AgentNode({ data }: { data: NodeData }) {
  const colors = CATEGORY_COLORS[data.category] || CATEGORY_COLORS.misc
  const dim = !data.enabled
  const liveState = data.live?.state
  const isActive = liveState === 'running' || liveState === 'starting'
  const glowRgb = STATE_GLOW_RGB[liveState || ''] || ''

  return (
    <div
      data-agent-id={data.id}
      data-state={liveState || 'idle'}
      style={{
        background: colors.bg,
        color: colors.text,
        border: `2px solid ${data.selected ? '#facc15' : colors.ring}`,
        borderRadius: 8,
        padding: '10px 14px',
        minWidth: 200,
        opacity: dim ? 0.55 : 1,
        boxShadow: data.selected
          ? '0 0 0 3px rgba(250, 204, 21, 0.3)'
          : isActive && glowRgb
          ? `0 0 0 3px rgba(${glowRgb}, 0.7), 0 0 24px 4px rgba(${glowRgb}, 0.55), 0 4px 12px rgba(0,0,0,0.4)`
          : '0 4px 12px rgba(0,0,0,0.4)',
        animation: isActive ? 'agent-node-pulse 1.4s ease-in-out infinite' : undefined,
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: colors.ring, width: 8, height: 8 }} />
      <div style={{ fontSize: 12, fontWeight: 600 }}>{data.name}</div>
      <div style={{ fontSize: 10, opacity: 0.7, fontFamily: 'monospace', marginTop: 2 }}>{data.id}</div>
      <div style={{ display: 'flex', gap: 8, marginTop: 6, fontSize: 9, flexWrap: 'wrap' }}>
        <span style={{ background: 'rgba(0,0,0,0.3)', padding: '1px 6px', borderRadius: 3 }}>{data.category}</span>
        {data.cron && <span style={{ background: 'rgba(0,0,0,0.3)', padding: '1px 6px', borderRadius: 3, fontFamily: 'monospace' }}>{data.cron}</span>}
        {!data.enabled && <span style={{ background: 'rgba(220,38,38,0.4)', padding: '1px 6px', borderRadius: 3 }}>disabled</span>}
        {isActive && (
          <span style={{ background: `rgba(${glowRgb}, 0.4)`, color: '#fff', padding: '1px 6px', borderRadius: 3, fontWeight: 600 }}>
            ● {liveState}
          </span>
        )}
      </div>
      {data.live?.current_action && isActive && (
        <div style={{ marginTop: 6, fontSize: 10, color: '#cbd5e1', fontStyle: 'italic',
                       maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis',
                       whiteSpace: 'nowrap' }}>
          ▸ {data.live.current_action}
        </div>
      )}
      {data.live && data.live.progress > 0 && data.live.progress < 1 && isActive && (
        <div style={{ marginTop: 4, height: 2, background: 'rgba(0,0,0,0.4)', borderRadius: 2 }}>
          <div style={{
            height: '100%',
            width: `${(data.live.progress * 100).toFixed(0)}%`,
            background: `rgb(${glowRgb})`,
            borderRadius: 2,
            transition: 'width 0.3s',
          }} />
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: colors.ring, width: 8, height: 8 }} />
    </div>
  )
}

const nodeTypes = { agent: AgentNode }

// ---------- ELK auto-layout ----------

const elk = new ELK()

async function layoutWithElk(nodes: Node[], edges: Edge[]): Promise<Record<string, { x: number; y: number }>> {
  const elkGraph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.spacing.nodeNode': '60',
      'elk.layered.spacing.nodeNodeBetweenLayers': '120',
    },
    children: nodes.map(n => ({ id: n.id, width: 240, height: 90 })),
    edges: edges.map(e => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  }
  const out = await elk.layout(elkGraph as any)
  const positions: Record<string, { x: number; y: number }> = {}
  for (const c of (out.children || [])) {
    if (c.x !== undefined && c.y !== undefined) positions[c.id] = { x: c.x, y: c.y }
  }
  return positions
}

// ---------- The page ----------

export default function GraphPage() {
  return (
    <ReactFlowProvider>
      <GraphInner />
    </ReactFlowProvider>
  )
}

function GraphInner() {
  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [legend, setLegend] = useState<{ id: string; label: string; style: string }[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [error, setError] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [savedAt, setSavedAt] = useState<string>('')
  const [statuses, setStatuses] = useState<Record<string, AgentLiveStatus>>({})
  const wsRefs = useRef<Map<string, WebSocket>>(new Map())
  const rfInstance = useRef<ReactFlowInstance | null>(null)
  const { setViewport } = useReactFlow()

  // Live-status WebSocket subscriptions — one per visible enabled agent.
  // When status arrives we splice it into the matching node's `data.live`
  // so the AgentNode component can glow + show current_action.
  useEffect(() => {
    const wantedIds = new Set(nodes.filter(n => (n.data as any).enabled).map(n => n.id))
    for (const [id, ws] of wsRefs.current) {
      if (!wantedIds.has(id)) {
        ws.close()
        wsRefs.current.delete(id)
      }
    }
    for (const id of wantedIds) {
      if (wsRefs.current.has(id)) continue
      const ws = openStatusWS(id, (status) => {
        setStatuses(s => ({ ...s, [id]: status }))
      })
      if (ws) wsRefs.current.set(id, ws)
    }
    return () => {
      for (const ws of wsRefs.current.values()) ws.close()
      wsRefs.current.clear()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes.length, nodes.map(n => n.id).join(',')])

  // Splice the latest statuses into the nodes' data.live so the custom
  // AgentNode renderer picks up the glow + current_action without
  // re-running the layout (just data update).
  useEffect(() => {
    setNodes(ns => ns.map(n => {
      const s = statuses[n.id]
      if (s === (n.data as any).live) return n
      return { ...n, data: { ...n.data, live: s } }
    }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statuses])

  // Pull persisted layout
  const loadLayout = (): { positions: Record<string, { x: number; y: number }>; viewport?: Viewport } => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return { positions: {} }
      return JSON.parse(raw)
    } catch { return { positions: {} } }
  }

  const saveLayout = useCallback(() => {
    if (!rfInstance.current) return
    const positions: Record<string, { x: number; y: number }> = {}
    for (const n of rfInstance.current.getNodes()) {
      positions[n.id] = { x: n.position.x, y: n.position.y }
    }
    const viewport = rfInstance.current.getViewport()
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ positions, viewport }))
    setSavedAt(new Date().toLocaleTimeString())
  }, [])

  // Initial fetch + layout
  useEffect(() => {
    let cancelled = false
    void (async () => {
      setLoading(true); setError('')
      try {
        const g = await api.dependencyGraph(false)
        if (cancelled) return
        const layout = loadLayout()

        const builtEdges: Edge[] = g.edges.map((e, i) => {
          const style = KIND_STYLES[e.kind] || KIND_STYLES['depends-on']
          return {
            id: `e-${i}-${e.from}-${e.to}-${e.kind}`,
            source: e.from,
            target: e.to,
            label: style.label || e.kind,
            type: 'smoothstep',
            animated: !!style.animated,
            style: { stroke: style.stroke, strokeWidth: 2, ...(style.strokeDasharray ? { strokeDasharray: style.strokeDasharray } : {}) },
            labelStyle: { fill: '#cbd5e1', fontSize: 10, fontFamily: 'monospace' },
            labelBgStyle: { fill: '#0f172a', fillOpacity: 0.85 },
            labelBgPadding: [4, 2],
            labelBgBorderRadius: 3,
            markerEnd: { type: MarkerType.ArrowClosed, color: style.stroke, width: 14, height: 14 },
            data: { kind: e.kind, description: e.description, default: e.default },
          }
        })

        // Determine positions: stored > else compute via ELK
        let positions = layout.positions
        const missing = g.nodes.filter(n => !positions[n.id])
        if (Object.keys(positions).length === 0 || missing.length > g.nodes.length / 2) {
          // Auto-layout
          const provisional: Node[] = g.nodes.map((n, i) => ({
            id: n.id,
            type: 'agent',
            position: { x: 0, y: i * 100 },
            data: { name: n.name, id: n.id, category: n.category, enabled: n.enabled, cron: n.cron, selected: false },
          }))
          positions = await layoutWithElk(provisional, builtEdges)
        }

        const builtNodes: Node[] = g.nodes.map((n, i) => ({
          id: n.id,
          type: 'agent',
          position: positions[n.id] || { x: (i % 4) * 280, y: Math.floor(i / 4) * 140 },
          data: { name: n.name, id: n.id, category: n.category, enabled: n.enabled, cron: n.cron, selected: false },
        }))

        setNodes(builtNodes)
        setEdges(builtEdges)
        setLegend(g.kinds)
        if (layout.viewport && rfInstance.current) {
          setViewport(layout.viewport as Viewport)
        }
      } catch (e: any) {
        setError(String(e?.message || e))
      } finally {
        setLoading(false)
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes(ns => applyNodeChanges(changes, ns))
  }, [])

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges(es => applyEdgeChanges(changes, es))
  }, [])

  const onConnect = useCallback(async (conn: Connection) => {
    if (!conn.source || !conn.target) return
    setEdges(es => addEdge({ ...conn, type: 'smoothstep', animated: false, label: 'depends on', style: { stroke: '#94a3b8', strokeWidth: 2 } }, es))
    // Persist as override on the target agent
    try {
      const cur = await api.getAgent(conn.target)
      const dep = ((cur as any).depends_on || []).filter((d: any) => d.agent_id !== conn.source).concat([
        { agent_id: conn.source, kind: 'depends-on', description: 'manual edge from graph UI' },
      ])
      await api.patchDependencies(conn.target, dep)
    } catch (e) {
      console.error('save edge failed', e)
    }
  }, [])

  const onNodeClick = useCallback((_e: React.MouseEvent, node: Node) => {
    setSelectedId(node.id)
    setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, selected: n.id === node.id } })))
  }, [])

  const autoLayout = async () => {
    if (!nodes.length) return
    const positions = await layoutWithElk(nodes, edges)
    setNodes(ns => ns.map(n => ({ ...n, position: positions[n.id] || n.position })))
    setTimeout(() => saveLayout(), 50)
  }

  const selectedNode = useMemo(() => nodes.find(n => n.id === selectedId), [nodes, selectedId])
  const selectedIncoming = useMemo(() => edges.filter(e => e.target === selectedId), [edges, selectedId])
  const selectedOutgoing = useMemo(() => edges.filter(e => e.source === selectedId), [edges, selectedId])

  return (
    <div className="h-[calc(100vh-180px)] md:h-[calc(100vh-65px)] w-full flex flex-col bg-surface-subtle text-ink-900 -mx-3 sm:-mx-5 -my-4 sm:-my-6">
      <div className="px-3 sm:px-4 py-2 border-b border-surface-divider flex flex-wrap items-center gap-2 sm:gap-3 bg-surface-page">
        <h1 className="text-sm font-semibold whitespace-nowrap">Agent Graph</h1>
        <span className="text-xs text-ink-500 hidden sm:inline">{nodes.length} agents · {edges.length} dependencies</span>
        <span className="text-xs text-ink-500 sm:hidden">{nodes.length}n / {edges.length}e</span>
        <div className="ml-auto flex items-center gap-1 sm:gap-2 text-xs">
          {savedAt && <span className="text-ink-500 hidden md:inline">saved {savedAt}</span>}
          <button onClick={saveLayout} aria-label="Save layout" className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded">💾<span className="hidden sm:inline ml-1">save</span></button>
          <button onClick={autoLayout} aria-label="Auto layout" className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded">⤴<span className="hidden sm:inline ml-1">auto</span></button>
          <button onClick={() => { localStorage.removeItem(STORAGE_KEY); window.location.reload() }} aria-label="Reset" className="px-2 py-1 bg-surface-subtle hover:bg-ink-200 rounded">⟲<span className="hidden sm:inline ml-1">reset</span></button>
        </div>
      </div>

      {error && <div className="px-4 py-2 bg-red-900/40 text-red-200 text-xs">{error}</div>}
      {loading && <div className="px-4 py-2 text-ink-500 text-xs">Loading dependency graph…</div>}

      <div className="flex-1 flex overflow-hidden">
        <div className="flex-1 relative">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onMoveEnd={saveLayout}
            onNodeDragStop={saveLayout}
            onInit={(inst) => { rfInstance.current = inst }}
            nodeTypes={nodeTypes}
            fitView={!loadLayout().viewport}
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.2}
            maxZoom={1.5}
            defaultEdgeOptions={{ type: 'smoothstep' }}
          >
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#1e293b" />
            <Controls className="!bg-surface-card border border-surface-divider !border-surface-divider" />
            <MiniMap
              nodeColor={(n) => {
                const cat = (n.data as any)?.category || 'misc'
                return CATEGORY_COLORS[cat]?.bg || '#374151'
              }}
              maskColor="rgba(15, 23, 42, 0.6)"
              style={{ background: '#0f172a', border: '1px solid #1e293b' }}
            />
            <Panel position="bottom-left" className="!bg-surface-page !border !border-surface-divider !rounded p-2 text-xs space-y-1">
              <div className="text-ink-400 font-semibold mb-1">Edge kinds</div>
              {legend.map(k => {
                const s = KIND_STYLES[k.id] || KIND_STYLES['depends-on']
                return (
                  <div key={k.id} className="flex items-center gap-2">
                    <svg width="36" height="6"><line x1="0" y1="3" x2="36" y2="3" stroke={s.stroke} strokeWidth="2" strokeDasharray={s.strokeDasharray} /></svg>
                    <span className="text-ink-600">{k.label}</span>
                  </div>
                )
              })}
            </Panel>
          </ReactFlow>
        </div>

        {/* Side panel — full-width slide-up sheet on mobile, fixed sidebar on desktop */}
        {selectedNode && (
          <aside className="
            fixed md:static inset-x-0 bottom-0 md:inset-auto z-30
            w-full md:w-80 max-h-[60vh] md:max-h-none
            border-t md:border-t-0 md:border-l border-surface-divider
            bg-surface-page p-4 overflow-auto text-xs
            shadow-2xl md:shadow-none
            rounded-t-xl md:rounded-none
            animate-slide-up md:animate-none
          ">
            <div className="flex items-start justify-between mb-3">
              <div>
                <div className="text-sm font-semibold text-ink-900">{(selectedNode.data as any).name}</div>
                <div className="font-mono text-ink-500 text-[11px] mt-0.5">{selectedNode.id}</div>
              </div>
              <button onClick={() => { setSelectedId(null); setNodes(ns => ns.map(n => ({ ...n, data: { ...n.data, selected: false } }))) }} className="text-ink-500 hover:text-ink-700">✕</button>
            </div>
            <Link to={`/agents/${selectedNode.id}`} className="block mb-3 text-status-running-fg underline">Open agent →</Link>

            <div className="mb-3 grid grid-cols-2 gap-2">
              <div><div className="text-[10px] uppercase text-ink-500">Category</div><div className="text-ink-700 mt-0.5">{(selectedNode.data as any).category}</div></div>
              <div><div className="text-[10px] uppercase text-ink-500">Cron</div><div className="font-mono text-ink-700 mt-0.5">{(selectedNode.data as any).cron || '(none)'}</div></div>
              <div><div className="text-[10px] uppercase text-ink-500">State</div><div className="text-ink-700 mt-0.5">{(selectedNode.data as any).enabled ? 'enabled' : 'disabled'}</div></div>
            </div>

            <div className="mb-3">
              <div className="text-[10px] uppercase text-ink-500 mb-1">Incoming ({selectedIncoming.length})</div>
              {selectedIncoming.length === 0 ? <div className="text-ink-600 italic">— none —</div> : selectedIncoming.map(e => (
                <div key={e.id} className="bg-surface-card border border-surface-divider rounded p-2 mb-1">
                  <div className="font-mono text-ink-600">{e.source}</div>
                  <div className="text-[10px] text-ink-500 mt-0.5">{(e.data as any).kind}</div>
                  <div className="text-ink-400 mt-1">{(e.data as any).description}</div>
                </div>
              ))}
            </div>

            <div>
              <div className="text-[10px] uppercase text-ink-500 mb-1">Outgoing ({selectedOutgoing.length})</div>
              {selectedOutgoing.length === 0 ? <div className="text-ink-600 italic">— none —</div> : selectedOutgoing.map(e => (
                <div key={e.id} className="bg-surface-card border border-surface-divider rounded p-2 mb-1">
                  <div className="font-mono text-ink-600">→ {e.target}</div>
                  <div className="text-[10px] text-ink-500 mt-0.5">{(e.data as any).kind}</div>
                  <div className="text-ink-400 mt-1">{(e.data as any).description}</div>
                </div>
              ))}
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
