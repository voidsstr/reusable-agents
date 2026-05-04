// App shell — responsive light theme. Desktop: horizontal top nav. Mobile:
// header with hamburger drawer + bottom tab bar (familiar mobile pattern,
// keeps nav reachable with a thumb).

import { BrowserRouter, NavLink, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { Suspense, lazy, useEffect, useState } from 'react'
import { setToken } from './api/client'

// Code-splitting: every route page becomes its own chunk so the initial
// bundle is just the shell + the landing route. AgentList is loaded
// eagerly because it's the default route and avoids a Suspense flash on
// the most common entry point. The rest defer until the user navigates
// to them, which on a typical session means most chunks are never
// downloaded at all.
import AgentList from './pages/AgentList'
const AgentDetail       = lazy(() => import('./pages/AgentDetail'))
const Confirmations     = lazy(() => import('./pages/Confirmations'))
const Events            = lazy(() => import('./pages/Events'))
const ImplementerQueue  = lazy(() => import('./pages/ImplementerQueue'))
const Providers         = lazy(() => import('./pages/Providers'))
const Graph             = lazy(() => import('./pages/Graph'))
const Goals             = lazy(() => import('./pages/Goals'))
const Settings          = lazy(() => import('./pages/Settings'))

type NavItem = { to: string; label: string; icon: string; mobile?: boolean }

const NAV: NavItem[] = [
  { to: '/',                   label: 'Agents',        icon: '⚙', mobile: true },
  { to: '/goals',              label: 'Goals',         icon: '🎯', mobile: true },
  { to: '/graph',              label: 'Graph',         icon: '◇' },
  { to: '/confirmations',      label: 'Inbox',         icon: '✉' },
  { to: '/implementer-queue',  label: 'Queue',         icon: '⚒', mobile: true },
  { to: '/providers',          label: 'AI',            icon: '🧠' },
  { to: '/events',             label: 'Events',        icon: '⏱' },
  { to: '/settings',           label: 'Settings',      icon: '⚙' },
]

const MOBILE_NAV = NAV.filter(n => n.mobile)

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/"                    element={<AgentList />} />
          <Route path="/agents/:id"          element={<Suspense fallback={<RouteSpinner />}><AgentDetail /></Suspense>} />
          <Route path="/goals"               element={<Suspense fallback={<RouteSpinner />}><Goals /></Suspense>} />
          <Route path="/goals/:agentId"      element={<Suspense fallback={<RouteSpinner />}><Goals /></Suspense>} />
          <Route path="/graph"               element={<Suspense fallback={<RouteSpinner />}><Graph /></Suspense>} />
          <Route path="/confirmations"       element={<Suspense fallback={<RouteSpinner />}><Confirmations /></Suspense>} />
          <Route path="/implementer-queue"   element={<Suspense fallback={<RouteSpinner />}><ImplementerQueue /></Suspense>} />
          <Route path="/providers"           element={<Suspense fallback={<RouteSpinner />}><Providers /></Suspense>} />
          <Route path="/events"              element={<Suspense fallback={<RouteSpinner />}><Events /></Suspense>} />
          <Route path="/settings"            element={<Suspense fallback={<RouteSpinner />}><Settings /></Suspense>} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

function RouteSpinner() {
  // Minimal placeholder shown only during the few hundred ms while a
  // route's chunk is fetched + parsed on first navigation to it.
  // No content shift — same vertical-space as the page header.
  return (
    <div className="flex items-center justify-center py-12 text-ink-400 text-sm" aria-live="polite">
      <span className="animate-pulse">Loading…</span>
    </div>
  )
}

function Layout() {
  const [tokenOpen, setTokenOpen] = useState(false)
  const [tokenInput, setTokenInput] = useState(localStorage.getItem('framework_api_token') ?? '')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const location = useLocation()

  // Close drawer on route change
  useEffect(() => { setDrawerOpen(false) }, [location.pathname])

  // Lock body scroll while drawer is open
  useEffect(() => {
    document.body.style.overflow = drawerOpen ? 'hidden' : ''
    return () => { document.body.style.overflow = '' }
  }, [drawerOpen])

  return (
    <div className="min-h-screen flex flex-col bg-surface-page">
      {/* ── Top header — responsive ────────────────────────────────────── */}
      <header
        className="bg-surface-card border-b border-surface-divider sticky top-0 z-30"
        style={{ paddingTop: 'env(safe-area-inset-top, 0)' }}
      >
        <div className="px-3 sm:px-5 h-14 flex items-center justify-between gap-2">
          {/* Left: hamburger (mobile) + brand */}
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => setDrawerOpen(o => !o)}
              className="md:hidden -ml-1 p-2 rounded-lg text-ink-700 hover:bg-surface-subtle active:bg-ink-200 transition-colors"
              aria-label="Open navigation"
              aria-expanded={drawerOpen}
            >
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                {drawerOpen ? (
                  <><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>
                ) : (
                  <><line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" /></>
                )}
              </svg>
            </button>
            <NavLink to="/" className="flex items-center gap-2 min-w-0">
              <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-accent-500 to-accent-700 flex items-center justify-center shadow-sm shrink-0">
                <span className="text-white font-bold text-sm">R</span>
              </div>
              <span className="font-semibold text-ink-900 text-[15px] tracking-tight truncate hidden sm:inline">
                reusable-agents
              </span>
            </NavLink>
          </div>

          {/* Center: desktop horizontal nav */}
          <nav className="hidden md:flex items-center gap-0.5 lg:gap-1 flex-1 justify-center">
            {NAV.map(n => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === '/'}
                className={({ isActive }) =>
                  `px-2.5 lg:px-3 py-1.5 rounded-md text-sm font-medium transition-colors whitespace-nowrap ${
                    isActive
                      ? 'text-accent-700 bg-accent-50'
                      : 'text-ink-600 hover:text-ink-900 hover:bg-surface-subtle'
                  }`
                }
              >
                <span className="mr-1.5 opacity-60 text-xs">{n.icon}</span>
                <span className="hidden lg:inline">{n.label}</span>
                <span className="lg:hidden">{n.label.slice(0, 6)}</span>
              </NavLink>
            ))}
          </nav>

          {/* Right: token */}
          <div className="flex items-center gap-2 shrink-0">
            {tokenOpen ? (
              <div className="flex items-center gap-1.5">
                <input
                  type="password"
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  placeholder="API token"
                  autoFocus
                  className="px-2.5 py-1.5 bg-surface-card border border-surface-divider rounded-md text-xs w-40 sm:w-56 focus:outline-none focus:border-accent-500 focus:ring-2 focus:ring-accent-500/20"
                />
                <button
                  onClick={() => { setToken(tokenInput); setTokenOpen(false); window.location.reload() }}
                  className="btn-primary"
                >save</button>
                <button
                  onClick={() => setTokenOpen(false)}
                  className="text-ink-500 hover:text-ink-700 text-xs px-1.5"
                >×</button>
              </div>
            ) : (
              <button
                onClick={() => setTokenOpen(true)}
                className="text-ink-500 hover:text-ink-800 text-sm sm:text-xs px-2 py-1 rounded-md hover:bg-surface-subtle"
                title="Set API token"
                aria-label="Set API token"
              >🔑</button>
            )}
          </div>
        </div>
      </header>

      {/* ── Mobile drawer ───────────────────────────────────────────────── */}
      {drawerOpen && (
        <>
          {/* Backdrop */}
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)}
            className="md:hidden fixed inset-0 bg-ink-900/40 backdrop-blur-sm z-30 animate-fade-in"
          />
          {/* Drawer panel */}
          <aside
            className="md:hidden fixed top-0 left-0 bottom-0 w-72 max-w-[85vw] bg-surface-card border-r border-surface-divider shadow-2xl z-40 animate-slide-in-left flex flex-col"
            style={{ paddingTop: 'env(safe-area-inset-top, 0)', paddingBottom: 'env(safe-area-inset-bottom, 0)' }}
          >
            <div className="h-14 flex items-center px-4 border-b border-surface-divider">
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-accent-500 to-accent-700 flex items-center justify-center shadow-sm">
                  <span className="text-white font-bold text-sm">R</span>
                </div>
                <span className="font-semibold text-ink-900 text-[15px]">reusable-agents</span>
              </div>
            </div>
            <nav className="flex-1 overflow-auto py-3 px-2">
              {NAV.map(n => (
                <NavLink
                  key={n.to}
                  to={n.to}
                  end={n.to === '/'}
                  className={({ isActive }) =>
                    `flex items-center gap-3 px-3 py-3 rounded-lg text-base font-medium transition-colors ${
                      isActive
                        ? 'text-accent-700 bg-accent-50'
                        : 'text-ink-700 hover:text-ink-900 hover:bg-surface-subtle active:bg-ink-100'
                    }`
                  }
                >
                  <span className="text-lg w-6 text-center opacity-70">{n.icon}</span>
                  {n.label}
                </NavLink>
              ))}
            </nav>
            <div className="border-t border-surface-divider px-4 py-3 text-[11px] text-ink-500">
              tap outside to close
            </div>
          </aside>
        </>
      )}

      {/* ── Main content ────────────────────────────────────────────────── */}
      <main className="flex-1 overflow-hidden">
        <div className="h-full overflow-auto">
          <div
            className="max-w-7xl mx-auto px-3 sm:px-5 py-4 sm:py-6 pb-24 md:pb-6"
            style={{ paddingBottom: 'max(1.5rem, calc(env(safe-area-inset-bottom, 0) + 5rem))' }}
          >
            <Outlet />
          </div>
        </div>
      </main>

      {/* ── Mobile bottom tab bar — primary destinations only ──────────── */}
      <nav
        className="md:hidden fixed bottom-0 inset-x-0 bg-surface-card border-t border-surface-divider z-20"
        style={{ paddingBottom: 'env(safe-area-inset-bottom, 0)' }}
        aria-label="Primary"
      >
        <div className="grid grid-cols-4 h-14">
          {MOBILE_NAV.map(n => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === '/'}
              className={({ isActive }) =>
                `flex flex-col items-center justify-center gap-0.5 text-[10px] font-medium transition-colors ${
                  isActive
                    ? 'text-accent-600'
                    : 'text-ink-500 hover:text-ink-800 active:bg-surface-subtle'
                }`
              }
            >
              <span className="text-lg leading-none">{n.icon}</span>
              <span className="leading-none">{n.label}</span>
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  )
}
