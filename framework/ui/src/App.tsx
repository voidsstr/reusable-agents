// Iframe-friendly app shell: minimal chrome, top-bar with refresh-interval
// control + token entry, page outlet.

import { BrowserRouter, NavLink, Outlet, Route, Routes } from 'react-router-dom'
import { useState } from 'react'
import { setToken } from './api/client'
import AgentList from './pages/AgentList'
import AgentDetail from './pages/AgentDetail'
import Confirmations from './pages/Confirmations'
import Events from './pages/Events'
import Providers from './pages/Providers'

const NAV = [
  { to: '/',              label: 'Agents' },
  { to: '/confirmations', label: 'Confirmations' },
  { to: '/providers',     label: 'AI Providers' },
  { to: '/events',        label: 'Events' },
]

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/"               element={<AgentList />} />
          <Route path="/agents/:id"     element={<AgentDetail />} />
          <Route path="/confirmations"  element={<Confirmations />} />
          <Route path="/providers"      element={<Providers />} />
          <Route path="/events"         element={<Events />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

function Layout() {
  const [tokenOpen, setTokenOpen] = useState(false)
  const [tokenInput, setTokenInput] = useState(localStorage.getItem('framework_api_token') ?? '')

  return (
    <div className="min-h-screen flex flex-col">
      <header className="px-4 py-3 border-b border-ink-800 flex items-center justify-between text-sm">
        <nav className="flex items-center gap-1">
          <span className="font-bold text-ink-100 mr-4">reusable-agents</span>
          {NAV.map(n => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === '/'}
              className={({ isActive }) =>
                `px-3 py-1 rounded transition-colors ${
                  isActive ? 'bg-ink-700 text-ink-50' : 'text-ink-300 hover:bg-ink-800'
                }`
              }
            >
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
                className="px-2 py-1 bg-ink-800 border border-ink-700 rounded text-xs w-56"
              />
              <button
                onClick={() => { setToken(tokenInput); setTokenOpen(false); window.location.reload() }}
                className="px-2 py-1 bg-glow-running text-ink-950 rounded text-xs font-semibold"
              >save</button>
              <button
                onClick={() => setTokenOpen(false)}
                className="px-2 py-1 text-ink-400 text-xs"
              >cancel</button>
            </div>
          ) : (
            <button
              onClick={() => setTokenOpen(true)}
              className="px-2 py-1 text-ink-400 hover:text-ink-200 text-xs"
            >🔑 token</button>
          )}
        </div>
      </header>
      <main className="flex-1 p-4 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}
