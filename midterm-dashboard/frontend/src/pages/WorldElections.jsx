import React, { useState, useEffect } from 'react'
import { Globe, RefreshCw, TrendingUp, ExternalLink } from 'lucide-react'
import { api } from '../lib/api'

const countryFlag = (code) => {
  if (!code || code.length !== 2) return '\u{1F30D}'
  return String.fromCodePoint(...[...code.toUpperCase()].map(c => 0x1F1E6 + c.charCodeAt(0) - 65))
}

const COUNTRIES = {
  UK: 'United Kingdom', GB: 'United Kingdom', FR: 'France', DE: 'Germany',
  CA: 'Canada', AU: 'Australia', BR: 'Brazil', MX: 'Mexico', IN: 'India',
  JP: 'Japan', KR: 'South Korea', IT: 'Italy', ES: 'Spain', NL: 'Netherlands',
  IL: 'Israel', TR: 'Turkey', AR: 'Argentina', CO: 'Colombia', PL: 'Poland',
  INTL: 'International',
}

const SOURCE_STYLES = {
  polymarket: { bg: 'bg-purple-100', text: 'text-purple-700', label: 'Polymarket' },
  kalshi: { bg: 'bg-blue-100', text: 'text-blue-700', label: 'Kalshi' },
}

function formatVolume(v) {
  if (!v) return null
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`
  return `$${v}`
}

function MarketCard({ market }) {
  const source = SOURCE_STYLES[market.source] || { bg: 'bg-stone-100', text: 'text-stone-600', label: market.source }
  const outcomes = market.outcomes || []
  const volume = market.volume || market.total_volume

  return (
    <div className="bg-white rounded-xl border border-stone-100 shadow-sm p-5 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between gap-3 mb-3">
        <h3 className="font-medium text-stone-900 text-sm leading-snug flex-1">
          {market.title || market.question}
        </h3>
        <span className={`${source.bg} ${source.text} px-2 py-0.5 rounded-full text-xs font-medium shrink-0`}>
          {source.label}
        </span>
      </div>

      <div className="space-y-2 mb-3">
        {outcomes.slice(0, 5).map((outcome, i) => {
          const prob = typeof outcome.probability === 'number'
            ? outcome.probability
            : typeof outcome.price === 'number'
              ? outcome.price
              : 0
          const pct = Math.round(prob * 100)
          return (
            <div key={i}>
              <div className="flex items-center justify-between text-xs mb-0.5">
                <span className="text-stone-700 truncate mr-2">{outcome.name || outcome.label}</span>
                <span className="font-semibold text-stone-900">{pct}%</span>
              </div>
              <div className="w-full bg-stone-100 rounded-full h-2">
                <div
                  className="h-2 rounded-full transition-all duration-500"
                  style={{
                    width: `${Math.max(pct, 1)}%`,
                    backgroundColor: i === 0 ? '#6366f1' : i === 1 ? '#f59e0b' : i === 2 ? '#10b981' : '#94a3b8',
                  }}
                />
              </div>
            </div>
          )
        })}
        {outcomes.length > 5 && (
          <p className="text-xs text-stone-400">+{outcomes.length - 5} more outcomes</p>
        )}
      </div>

      {volume && (
        <div className="flex items-center gap-1 text-xs text-stone-400">
          <TrendingUp className="h-3 w-3" />
          <span>{formatVolume(volume)} volume</span>
        </div>
      )}
    </div>
  )
}

export default function WorldElections() {
  const [markets, setMarkets] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [countryFilter, setCountryFilter] = useState('all')
  const [sourceFilter, setSourceFilter] = useState('all')

  const fetchData = async () => {
    try {
      setLoading(true)
      setError(null)
      const data = await api.worldElections()
      setMarkets(data.markets || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchData() }, [])

  const filtered = markets.filter(m => {
    const matchesSearch = !search || (m.title || '').toLowerCase().includes(search.toLowerCase())
    const matchesCountry = countryFilter === 'all' || (m.state || 'INTL') === countryFilter
    const matchesSource = sourceFilter === 'all' || m.source === sourceFilter
    return matchesSearch && matchesCountry && matchesSource
  })

  const allCountries = ['all', ...[...new Set(markets.map(m => m.state || 'INTL'))].sort()]
  const allSources = ['all', ...new Set(markets.map(m => m.source).filter(Boolean))]

  // Group by country
  const grouped = {}
  for (const m of filtered) {
    const code = m.state || 'INTL'
    if (!grouped[code]) grouped[code] = []
    grouped[code].push(m)
  }

  // Sort country groups by number of markets (most first), INTL last
  const sortedCountries = Object.keys(grouped).sort((a, b) => {
    if (a === 'INTL') return 1
    if (b === 'INTL') return -1
    return grouped[b].length - grouped[a].length
  })

  return (
    <div>
      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-2">
          <div className="p-2 bg-indigo-50 rounded-lg">
            <Globe className="h-6 w-6 text-indigo-600" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-stone-900 tracking-tight">World Elections</h1>
            <p className="text-stone-500 text-sm">
              Tracking global leader elections via prediction markets
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3 mt-4">
          <button
            onClick={fetchData}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-stone-600 hover:text-stone-800 bg-white border border-stone-200 rounded-lg hover:bg-stone-50 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
          <span className="text-xs text-stone-400">
            {filtered.length} of {markets.length} market{markets.length !== 1 ? 's' : ''}
          </span>
        </div>

        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 mt-4 grid grid-cols-1 md:grid-cols-3 gap-3">
          <input type="text" placeholder="Search markets..." value={search} onChange={e => setSearch(e.target.value)}
            className="bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-800 focus:outline-none focus:ring-2 focus:ring-stone-900/10" />
          <select value={countryFilter} onChange={e => setCountryFilter(e.target.value)}
            className="bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
            {allCountries.map(c => <option key={c} value={c}>{c === 'all' ? 'All countries' : (COUNTRIES[c] || c)}</option>)}
          </select>
          <select value={sourceFilter} onChange={e => setSourceFilter(e.target.value)}
            className="bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
            {allSources.map(s => <option key={s} value={s}>{s === 'all' ? 'All sources' : s}</option>)}
          </select>
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div className="bg-red-50 border border-red-100 rounded-xl p-4 mb-6 text-sm text-red-700">
          Failed to load world election data: {error}
        </div>
      )}

      {/* Loading state */}
      {loading && markets.length === 0 && (
        <div className="flex items-center justify-center py-20">
          <div className="animate-spin rounded-full h-6 w-6 border-2 border-stone-300 border-t-stone-800"></div>
        </div>
      )}

      {/* Empty state */}
      {!loading && markets.length === 0 && !error && (
        <div className="bg-white rounded-xl border border-stone-100 shadow-sm p-12 text-center">
          <Globe className="h-12 w-12 text-stone-300 mx-auto mb-4" />
          <h2 className="text-lg font-semibold text-stone-700 mb-2">No world election data yet</h2>
          <p className="text-stone-400 text-sm max-w-md mx-auto">
            World election data refreshes every 5 minutes. Check back shortly as markets are
            fetched from Polymarket and Kalshi.
          </p>
        </div>
      )}

      {/* Country sections */}
      {sortedCountries.map(code => (
        <section key={code} className="mb-8">
          <div className="flex items-center gap-2 mb-4">
            <span className="text-xl">{countryFlag(code)}</span>
            <h2 className="text-lg font-semibold text-stone-800">
              {COUNTRIES[code] || code}
            </h2>
            <span className="text-xs text-stone-400 bg-stone-100 px-2 py-0.5 rounded-full">
              {grouped[code].length} market{grouped[code].length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {grouped[code].map((m, i) => (
              <MarketCard key={m.market_id || `${code}-${i}`} market={m} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
