import React, { useState, useEffect, createContext, useContext } from 'react'
import { Routes, Route, Navigate, Link, useLocation, useNavigate } from 'react-router-dom'
import { api } from './lib/api'
import { SettingsProvider } from './lib/settings'
import { useT } from './lib/i18n'
import { BarChart3, GitCompare, Shield, LogOut, Menu, X, Crown, Home, Activity, User, Globe, History, Bell, Briefcase } from 'lucide-react'

import ErrorBoundary from './lib/ErrorBoundary'
import Dashboard from './pages/Dashboard'
import Races from './pages/Races'
import RaceDetail from './pages/RaceDetail'
import Divergence from './pages/Divergence'
import Compare from './pages/Compare'
import Login from './pages/Login'
import Register from './pages/Register'
import AdminDashboard from './pages/AdminDashboard'
import Account from './pages/Account'
import WorldElections from './pages/WorldElections'
import Historical from './pages/Historical'
import Settings from './pages/Settings'
import Notifications from './pages/Notifications'
import Portfolio from './pages/Portfolio'

const AuthContext = createContext(null)
export const useAuth = () => useContext(AuthContext)

function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null)).finally(() => setLoading(false))
    const handler = () => setUser(null)
    window.addEventListener('auth:unauthorized', handler)
    return () => window.removeEventListener('auth:unauthorized', handler)
  }, [])

  const login = async (email, password) => {
    const data = await api.login(email, password)
    setUser(data.user)
    return data
  }

  const register = async (email, password, name) => {
    const data = await api.register(email, password, name)
    setUser(data.user)
    return data
  }

  const logout = async () => {
    await api.logout().catch(() => {})
    setUser(null)
  }

  if (loading) return (
    <div className="min-h-screen flex items-center justify-center bg-stone-50"
      role="status" aria-live="polite" aria-label="Loading dashboard">
      <div className="animate-spin rounded-full h-6 w-6 border-2 border-stone-300 border-t-stone-800"></div>
      <span className="sr-only">Loading…</span>
    </div>
  )

  return (
    <AuthContext.Provider value={{ user, login, register, logout, setUser }}>
      <SettingsProvider user={user}>
        {children}
      </SettingsProvider>
    </AuthContext.Provider>
  )
}

function Nav() {
  const { user, logout } = useAuth()
  const location = useLocation()
  const t = useT()
  const [mobileOpen, setMobileOpen] = useState(false)

  const links = [
    { to: '/', label: t('nav.overview'), icon: Home },
    { to: '/races', label: t('nav.races'), icon: BarChart3 },
    { to: '/compare', label: 'Compare', icon: GitCompare },
    { to: '/divergence', label: t('nav.divergence'), icon: GitCompare },
    { to: '/world', label: t('nav.world'), icon: Globe },
    { to: '/historical', label: t('nav.historical'), icon: History },
  ]

  if (user) {
    links.push({ to: '/notifications', label: 'Alerts', icon: Bell })
  }
  if (user && (user.tier === 'premium' || user.tier === 'admin')) {
    links.push({ to: '/portfolio', label: 'Portfolio', icon: Briefcase })
  }
  if (user?.tier === 'admin') {
    links.push({ to: '/admin', label: t('nav.admin'), icon: Shield })
  }

  const isActive = (path) => location.pathname === path

  return (
    <nav className="bg-white/80 backdrop-blur-xl border-b border-stone-200/60 sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-6">
        <div className="flex items-center justify-between h-14">
          <Link to="/" className="flex items-center gap-2 group">
            <Activity className="h-5 w-5 text-stone-800 group-hover:text-stone-600 transition-colors" />
            <span className="font-semibold text-stone-900 tracking-tight">MidtermEdge</span>
          </Link>

          <div className="hidden md:flex items-center gap-0.5">
            {links.map(l => (
              <Link key={l.to} to={l.to}
                className={`px-3 py-1.5 rounded-md text-sm transition-all ${
                  isActive(l.to)
                    ? 'text-stone-900 bg-stone-100 font-medium'
                    : 'text-stone-500 hover:text-stone-800 hover:bg-stone-50'
                }`}>
                {l.label}
              </Link>
            ))}
          </div>

          <div className="hidden md:flex items-center gap-3">
            {user ? (
              <>
                {user.tier === 'premium' && <span className="badge-premium">Premium</span>}
                {user.tier === 'admin' && <span className="bg-stone-100 text-stone-700 px-2.5 py-0.5 rounded-full text-xs font-medium">Admin</span>}
                <Link to="/account" className="text-sm text-stone-500 hover:text-stone-800 transition-colors">{user.display_name || user.email}</Link>
                <button onClick={logout} className="text-stone-400 hover:text-stone-600 transition-colors"><LogOut className="h-4 w-4" /></button>
              </>
            ) : (
              <>
                <Link to="/login" className="text-sm text-stone-500 hover:text-stone-800 transition-colors">{t('nav.login')}</Link>
                <Link to="/register" className="btn-primary text-sm">{t('nav.signup')}</Link>
              </>
            )}
          </div>

          <button className="md:hidden text-stone-500" onClick={() => setMobileOpen(!mobileOpen)}
            aria-label={mobileOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={mobileOpen} aria-controls="mobile-nav">
            {mobileOpen ? <X className="h-5 w-5" aria-hidden="true" /> : <Menu className="h-5 w-5" aria-hidden="true" />}
          </button>
        </div>

        {mobileOpen && (
          <div id="mobile-nav" className="md:hidden pb-4 pt-2 space-y-1 border-t border-stone-100">
            {links.map(l => (
              <Link key={l.to} to={l.to} onClick={() => setMobileOpen(false)}
                className={`block px-3 py-2 rounded-lg text-sm ${
                  isActive(l.to) ? 'text-stone-900 bg-stone-100 font-medium' : 'text-stone-500'
                }`}>
                {l.label}
              </Link>
            ))}
            {user ? (
              <button onClick={() => { logout(); setMobileOpen(false) }} className="block px-3 py-2 text-sm text-stone-500 w-full text-left">
                {t('nav.logout')}
              </button>
            ) : (
              <div className="flex gap-2 px-3 pt-2">
                <Link to="/login" onClick={() => setMobileOpen(false)} className="btn-secondary flex-1 text-center text-sm">{t('nav.login')}</Link>
                <Link to="/register" onClick={() => setMobileOpen(false)} className="btn-primary flex-1 text-center text-sm">{t('nav.signup')}</Link>
              </div>
            )}
          </div>
        )}
      </div>
    </nav>
  )
}

function ProtectedRoute({ children, tier }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" />
  if (tier && user.tier !== tier && user.tier !== 'admin') return <Navigate to="/" />
  return children
}

export default function App() {
  return (
    <AuthProvider>
      <div className="min-h-screen bg-stone-50">
        <Nav />
        <main className="max-w-6xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
          <ErrorBoundary>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/races" element={<Races />} />
              <Route path="/race/:raceKey" element={<RaceDetail />} />
              <Route path="/compare" element={<Compare />} />
              <Route path="/divergence" element={<Divergence />} />
              <Route path="/world" element={<WorldElections />} />
              <Route path="/historical" element={<Historical />} />
              <Route path="/login" element={<Login />} />
              <Route path="/register" element={<Register />} />
              <Route path="/account" element={<ProtectedRoute><Account /></ProtectedRoute>} />
              <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
              <Route path="/notifications" element={<ProtectedRoute><Notifications /></ProtectedRoute>} />
              <Route path="/portfolio" element={<ProtectedRoute tier="premium"><Portfolio /></ProtectedRoute>} />
              <Route path="/admin" element={<ProtectedRoute tier="admin"><AdminDashboard /></ProtectedRoute>} />
            </Routes>
          </ErrorBoundary>
        </main>
        <footer className="border-t border-stone-200/60 mt-16 py-8 text-center text-stone-400 text-sm">
          <span className="text-stone-500">MidtermEdge</span> &middot; Prediction market data for informational purposes only.
        </footer>
      </div>
    </AuthProvider>
  )
}
