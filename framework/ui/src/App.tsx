// Iframe-friendly app shell: light theme, generous whitespace, Linear-style
// minimal chrome. Top nav uses underline-on-active accent; token entry is
// tucked away as a discreet button.

import { BrowserRouter, NavLink, Outlet, Route, Routes } from 'react-router-dom'
import { useState } from 'react'
import { setToken } from './api/client'
import AgentList from './pages/AgentList'
import AgentDetail from './pages/AgentDetail'
import Confirmations from './pages/Confirmations'
import Events from './pages/Events'
import Providers from './pages/Providers'
import Graph from './pages/Graph'

const NAV = [
  { to: '/',              label: 'Agents',        icon: '⚙' },
  { to: '/graph',         label: 'Graph',         icon: '◇' },
  { to: '/confirmations', label: 'Confirmations', icon: '✉' },
  { to: '/providers',     label: 'AI Providers',  icon: '🧠' },
  { to: '/events',        label: 'Events',        icon: '⏱' },
]

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/"                element={<AgentList />} />
          <Route path="/agents/:id"      element={<AgentDetail />} />
          <Route path="/graph"           element={<Graph />} />
          <Route path="/confirmations"   element={<Confirmations />} />
          <Route path="/providers"       element={<Providers />} />
          <Route path="/events"          element={<Events />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

function Layout() {
  const [tokenOpen, setTokenOpen] = useState(false)
  const [tokenInput, setTokenInput] = useState(localStorage.getItem('framework_api_token') ?? '')

  return (
    <div className="min-h-screen flex flex-col bg-surface-page">
      <header className="bg-surface-card border-b border-surface-divider sticky top-0 z-20">
        <div className="px-5 py-2.5 flex items-center justify-between">
          <nav className="flex items-center gap-1">
            <div className="flex items-center gap-2 mr-5">
              <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-accent-500 to-accent-700 flex items-center justify-center shadow-sm">
                <span className="text-white font-bold text-sm">R</span>
              </div>
              <span className="font-semibold text-ink-900 text-[15px] tracking-tight">
                reusable-agents
              </span>
            </div>
            {NAV.map(n => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === '/'}
                className={({ isActive }) =>
                  `px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                    isActive
                      ? 'text-accent-700 bg-accent-50'
                      : 'text-ink-600 hover:text-ink-900 hover:bg-surface-subtle'
                  }`
                }
              >
                <span className="mr-1.5 opacity-60 text-xs">{n.icon}</span>
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-2">
            {tokenOpen ? (
              <div className="flex items-center gap-2">
                <input
                  type="password"
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  placeholder="API token"
                  className="px-2.5 py-1.5 bg-surface-card border border-surface-divider rounded-md text-xs w-56 focus:outline-none focus:border-accent-500 focus:ring-2 focus:ring-accent-500/20"
                />
                <button
                  onClick={() => { setToken(tokenInput); setTokenOpen(false); window.location.reload() }}
                  className="btn-primary"
                >save</button>
                <button
                  onClick={() => setTokenOpen(false)}
                  className="text-ink-500 hover:text-ink-700 text-xs px-2"
                >cancel</button>
              </div>
            ) : (
              <button
                onClick={() => setTokenOpen(true)}
                className="text-ink-500 hover:text-ink-800 text-xs px-2 py-1 rounded-md hover:bg-surface-subtle"
                title="Set API token"
              >🔑 token</button>
            )}
          </div>
        </div>
      </header>
      <main className="flex-1 overflow-hidden">
        <div className="h-full overflow-auto">
          <div className="max-w-7xl mx-auto px-5 py-6">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  )
}
