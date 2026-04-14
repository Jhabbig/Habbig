import React, { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { api } from './api'
import { ensureRates, formatUSDAs, formatUSDAsCompact, convertUSD, CURRENCIES } from './currency'

export { CURRENCIES }

const SettingsContext = createContext(null)
export const useSettings = () => useContext(SettingsContext)

// ---------------------------------------------------------------------------
// Unit-system helpers (American $/en-US vs European €/de-DE)
// ---------------------------------------------------------------------------
// We read from localStorage rather than the React context so these helpers
// can be used at module top-level without a hook. They keep their results
// in sync with whatever the SettingsProvider has just written to storage.

function _readSettings() {
  try {
    const cached = localStorage.getItem('midterm_settings')
    if (cached) return JSON.parse(cached) || {}
  } catch {}
  return {}
}

function _getUnitSystem() {
  const s = _readSettings()
  if (s.unitSystem === 'european' || s.unitSystem === 'american') return s.unitSystem
  return 'american'
}

function _getCurrencyCode() {
  const s = _readSettings()
  if (typeof s.currency === 'string' && /^[A-Z]{3}$/.test(s.currency)) return s.currency
  return 'USD'
}

export function isMetric(unitSystem) {
  const sys = unitSystem || _getUnitSystem()
  return sys === 'european'
}

export function getLocale(unitSystem) {
  return isMetric(unitSystem) ? 'de-DE' : 'en-US'
}

export function getCurrencyCode(currency) {
  return currency || _getCurrencyCode()
}

// Legacy: still used by components that just want a symbol next to a value.
// We resolve it from the chosen currency via Intl.
export function getCurrencySymbol(unitSystem, currency) {
  const code = getCurrencyCode(currency)
  try {
    const parts = new Intl.NumberFormat(getLocale(unitSystem), {
      style: 'currency',
      currency: code,
      maximumFractionDigits: 0,
    }).formatToParts(0)
    const sym = parts.find(p => p.type === 'currency')
    if (sym) return sym.value
  } catch {}
  return code
}

export function fmtNum(n, unitSystem) {
  if (n == null || isNaN(n)) return '\u2014'
  return Number(n).toLocaleString(getLocale(unitSystem))
}

// fmtMoney: input is a USD amount; we convert + format in the user's currency.
export function fmtMoney(usdAmount, unitSystem, currency) {
  if (usdAmount == null || isNaN(usdAmount)) return '\u2014'
  return formatUSDAs(usdAmount, getCurrencyCode(currency), getLocale(unitSystem))
}

// fmtVolume: compact USD-input formatter (input in USD, output in user currency).
export function fmtVolume(usdValue, unitSystem, currency) {
  if (!usdValue && usdValue !== 0) return null
  return formatUSDAsCompact(usdValue, getCurrencyCode(currency), getLocale(unitSystem))
}

// Compact integer count (no currency): 1.2M / 1,2M
export function fmtCount(v, unitSystem) {
  if (v == null || isNaN(v)) return '\u2014'
  const loc = getLocale(unitSystem)
  if (v >= 1_000_000) return `${(v / 1_000_000).toLocaleString(loc, { maximumFractionDigits: 2 })}M`
  if (v >= 1_000) return `${(v / 1_000).toLocaleString(loc, { maximumFractionDigits: 0 })}K`
  return Number(v).toLocaleString(loc)
}

export function fmtDate(dateStr, unitSystem) {
  if (!dateStr) return '\u2014'
  try {
    const d = typeof dateStr === 'string' ? new Date(dateStr) : dateStr
    if (isNaN(d.getTime())) return String(dateStr)
    return d.toLocaleDateString(getLocale(unitSystem))
  } catch {
    return String(dateStr)
  }
}

// React hook variant — returns formatters that are reactive to setting changes
export function useUnits() {
  const ctx = useContext(SettingsContext)
  const sys = ctx?.settings?.unitSystem || 'american'
  const cur = ctx?.settings?.currency || 'USD'
  return {
    unitSystem: sys,
    isMetric: sys === 'european',
    locale: getLocale(sys),
    currencyCode: cur,
    currency: getCurrencySymbol(sys, cur),
    fmtNum: (n) => fmtNum(n, sys),
    fmtMoney: (a) => fmtMoney(a, sys, cur),
    fmtVolume: (v) => fmtVolume(v, sys, cur),
    fmtCount: (v) => fmtCount(v, sys),
    fmtDate: (d) => fmtDate(d, sys),
    convertUSD: (a) => convertUSD(a, cur),
  }
}

const ACCENT_MAP = {
  blue:    { hex: '#3b82f6', hover: '#2563eb', light: '#eff6ff' },
  rose:    { hex: '#f43f5e', hover: '#e11d48', light: '#fff1f2' },
  amber:   { hex: '#f59e0b', hover: '#d97706', light: '#fffbeb' },
  emerald: { hex: '#10b981', hover: '#059669', light: '#ecfdf5' },
  violet:  { hex: '#8b5cf6', hover: '#7c3aed', light: '#f5f3ff' },
  orange:  { hex: '#f97316', hover: '#ea580c', light: '#fff7ed' },
  cyan:    { hex: '#06b6d4', hover: '#0891b2', light: '#ecfeff' },
  stone:   { hex: '#78716c', hover: '#57534e', light: '#fafaf9' },
}

const DEFAULTS = {
  theme: 'light',
  accentColor: 'blue',
  contrast: 'normal',
  density: 'comfortable',
  chartStyle: 'line',
  dataSources: ['polymarket', 'kalshi', 'predictit', 'polling'],
  defaultHistory: 30,
  showPollingOnCharts: true,
  unitSystem: 'american', // 'american' = en-US format, 'european' = de-DE format
  currency: 'USD',         // 3-letter ISO code; converts USD-denominated values
  language: 'en',          // UI language (en, es, de, fr, it, pt, nl, pl, ja, ko, zh, ru)
}

function applyToDOM(settings) {
  const root = document.documentElement

  // Theme
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  const isDark = settings.theme === 'dark' || (settings.theme === 'system' && prefersDark)
  root.setAttribute('data-theme', isDark ? 'dark' : 'light')

  // Accent color
  const accent = ACCENT_MAP[settings.accentColor] || ACCENT_MAP.blue
  root.style.setProperty('--accent', accent.hex)
  root.style.setProperty('--accent-hover', accent.hover)
  root.style.setProperty('--accent-light', accent.light)

  // Contrast
  root.setAttribute('data-contrast', settings.contrast || 'normal')

  // Density
  root.setAttribute('data-density', settings.density || 'comfortable')

  // Language (sets <html lang> for accessibility + browser hinting)
  if (settings.language) root.lang = settings.language
}

export function SettingsProvider({ children, user }) {
  const [settings, setSettings] = useState(() => {
    // Try localStorage first for instant load
    try {
      const cached = localStorage.getItem('midterm_settings')
      if (cached) return { ...DEFAULTS, ...JSON.parse(cached) }
    } catch {}
    return DEFAULTS
  })

  // Load from API if logged in
  useEffect(() => {
    if (user) {
      // User object from /auth/me may already have settings
      if (user.settings && Object.keys(user.settings).length > 0) {
        setSettings(prev => {
          const merged = { ...prev, ...user.settings }
          localStorage.setItem('midterm_settings', JSON.stringify(merged))
          return merged
        })
      } else {
        api.getSettings().then(data => {
          const s = data?.settings || data
          if (s && Object.keys(s).length > 0) {
            setSettings(prev => {
              const merged = { ...prev, ...s }
              localStorage.setItem('midterm_settings', JSON.stringify(merged))
              return merged
            })
          }
        }).catch(() => {})
      }
    }
  }, [user])

  // Apply settings to DOM whenever they change
  useEffect(() => {
    applyToDOM(settings)
  }, [settings])

  // Track FX-rate freshness. After the rates land, we bump fxVersion in the
  // context so components subscribed to it re-render with the converted
  // values. (We don't store the rates themselves — currency.js caches them
  // in localStorage and the formatters read from there synchronously.)
  const [fxVersion, setFxVersion] = useState(0)
  useEffect(() => {
    let cancelled = false
    ensureRates().then(() => {
      if (!cancelled) setFxVersion(n => n + 1)
    })
    return () => { cancelled = true }
  }, [settings.currency])

  // Listen for system theme changes
  useEffect(() => {
    if (settings.theme !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => applyToDOM(settings)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [settings.theme])

  const updateSetting = useCallback((key, value) => {
    setSettings(prev => {
      const next = { ...prev, [key]: value }
      localStorage.setItem('midterm_settings', JSON.stringify(next))
      // Persist to backend if logged in
      api.updateSettings({ [key]: value }).catch(() => {})
      return next
    })
  }, [])

  const updateSettings = useCallback((updates) => {
    setSettings(prev => {
      const next = { ...prev, ...updates }
      localStorage.setItem('midterm_settings', JSON.stringify(next))
      api.updateSettings(updates).catch(() => {})
      return next
    })
  }, [])

  return (
    <SettingsContext.Provider value={{ settings, updateSetting, updateSettings, fxVersion }}>
      {children}
    </SettingsContext.Provider>
  )
}
