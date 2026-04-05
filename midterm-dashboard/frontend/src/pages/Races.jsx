import React, { useState, useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { Search, ArrowRight, MapPin, X, History } from 'lucide-react'

const SOURCE_STYLES = {
  polymarket: { bg: 'bg-purple-100', text: 'text-purple-700', bar: '#a855f7', label: 'Polymarket' },
  kalshi: { bg: 'bg-blue-100', text: 'text-blue-700', bar: '#3b82f6', label: 'Kalshi' },
  predictit: { bg: 'bg-amber-100', text: 'text-amber-700', bar: '#f59e0b', label: 'PredictIt' },
}

const PARTY_COLORS = {
  D: 'bg-blue-100 text-blue-700',
  R: 'bg-red-100 text-red-700',
  I: 'bg-purple-100 text-purple-700',
}

// Pick the "headline" probability for a source: highest outcome with a probability
function topProb(market) {
  const outcomes = (market.outcomes || []).filter(o => typeof o.probability === 'number')
  if (!outcomes.length) return null
  const sorted = [...outcomes].sort((a, b) => (b.probability || 0) - (a.probability || 0))
  return sorted[0]
}

function SourceColumn({ source, market }) {
  const style = SOURCE_STYLES[source] || { bg: 'bg-stone-100', text: 'text-stone-700', bar: '#78716c', label: source }
  const top = market ? topProb(market) : null
  return (
    <div className="flex-1 min-w-0 border border-stone-100 rounded-lg p-3 bg-stone-50/50">
      <div className="flex items-center justify-between mb-2">
        <span className={`${style.bg} ${style.text} text-[10px] font-bold px-2 py-0.5 rounded uppercase tracking-wide`}>{style.label}</span>
        {market?.volume > 0 && <span className="text-[10px] text-stone-400">${(market.volume / 1000).toFixed(0)}k</span>}
      </div>
      {top ? (
        <>
          <div className="flex items-baseline justify-between mb-1">
            <span className="text-[11px] text-stone-600 truncate mr-2" title={top.name}>{top.name}</span>
            <span className="text-lg font-bold tabular-nums" style={{ color: style.bar }}>{Math.round((top.probability || 0) * 100)}%</span>
          </div>
          <div className="w-full bg-stone-100 rounded-full h-1.5">
            <div className="h-1.5 rounded-full" style={{ width: `${Math.max((top.probability || 0) * 100, 1)}%`, backgroundColor: style.bar }} />
          </div>
        </>
      ) : (
        <div className="text-xs text-stone-300 italic py-2">no data</div>
      )}
    </div>
  )
}

function DeltaBadge({ polymarket, kalshi }) {
  const p = polymarket ? topProb(polymarket) : null
  const k = kalshi ? topProb(kalshi) : null
  if (!p || !k) return null
  const delta = Math.abs((p.probability || 0) - (k.probability || 0)) * 100
  if (delta < 0.5) return null
  const color = delta > 10 ? 'bg-red-100 text-red-700' : delta > 5 ? 'bg-amber-100 text-amber-700' : 'bg-emerald-100 text-emerald-700'
  return (
    <div className={`flex flex-col items-center justify-center px-3 py-2 rounded-lg ${color} shrink-0`}>
      <span className="text-[9px] font-bold uppercase tracking-wider opacity-70">Δ Spread</span>
      <span className="text-base font-bold tabular-nums">{delta.toFixed(1)}%</span>
    </div>
  )
}

export default function Races() {
  const [races, setRaces] = useState([])
  const [historical, setHistorical] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [sourceFilter, setSourceFilter] = useState('all')
  const [stateFilter, setStateFilter] = useState('all')
  const [minVolume, setMinVolume] = useState(0)

  useEffect(() => {
    Promise.all([
      api.races().then(d => Array.isArray(d) ? d : d?.races || []),
      api.historical().then(d => d?.results || []).catch(() => []),
    ])
      .then(([r, h]) => { setRaces(r); setHistorical(h) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Group markets by race_key so we can show all sources for a race on one row
  const grouped = useMemo(() => {
    const map = new Map()
    for (const m of races) {
      const key = m.race_key || `${m.race_type || 'other'}_${m.state || 'US'}`
      if (!map.has(key)) {
        map.set(key, {
          race_key: key,
          race_type: m.race_type,
          state: m.state,
          title: m.title || m.event_title,
          event_title: m.event_title,
          sources: {},
          volume: 0,
        })
      }
      const g = map.get(key)
      g.sources[m.source] = m
      g.volume += m.volume || 0
      if (!g.title) g.title = m.title || m.event_title
    }
    return Array.from(map.values())
  }, [races])

  // Index historical by race_type+state for fast lookup
  const histIndex = useMemo(() => {
    const idx = new Map()
    for (const h of historical) {
      const key = `${h.race_type}_${h.state}`
      if (!idx.has(key)) idx.set(key, [])
      idx.get(key).push(h)
    }
    // Sort each bucket by year desc
    for (const v of idx.values()) v.sort((a, b) => b.year - a.year)
    return idx
  }, [historical])

  const filtered = useMemo(() => grouped.filter(g => {
    const matchesSearch = !search || (g.title || '').toLowerCase().includes(search.toLowerCase()) || (g.state || '').toLowerCase().includes(search.toLowerCase())
    const matchesType = typeFilter === 'all' || g.race_type === typeFilter
    const matchesSource = sourceFilter === 'all' || sourceFilter in g.sources
    const matchesState = stateFilter === 'all' || g.state === stateFilter
    const matchesVolume = g.volume >= minVolume
    return matchesSearch && matchesType && matchesSource && matchesState && matchesVolume
  }), [grouped, search, typeFilter, sourceFilter, stateFilter, minVolume])

  const types = ['all', ...new Set(grouped.map(r => r.race_type).filter(Boolean))]
  const sources = ['all', ...new Set(races.map(r => r.source).filter(Boolean))]
  const states = ['all', ...[...new Set(grouped.map(r => r.state).filter(Boolean))].sort()]
  const hasFilters = search || typeFilter !== 'all' || sourceFilter !== 'all' || stateFilter !== 'all' || minVolume > 0

  const clearFilters = () => {
    setSearch(''); setTypeFilter('all'); setSourceFilter('all'); setStateFilter('all'); setMinVolume(0)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-semibold text-stone-800">All Races</h1>
        <span className="text-sm text-stone-400">{filtered.length} of {grouped.length}</span>
      </div>

      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 mb-4 space-y-3">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-stone-400" />
          <input type="text" placeholder="Search races..." value={search} onChange={e => setSearch(e.target.value)}
            className="w-full bg-stone-50 border border-stone-200 rounded-lg pl-10 pr-4 py-2 text-sm text-stone-800 focus:outline-none focus:ring-2 focus:ring-stone-900/10" />
        </div>

        <div className="flex flex-wrap gap-1">
          {types.map(t => (
            <button key={t} onClick={() => setTypeFilter(t)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize transition-colors ${typeFilter === t ? 'bg-stone-800 text-white' : 'bg-stone-50 text-stone-500 hover:bg-stone-100'}`}>
              {t}
            </button>
          ))}
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div>
            <label className="text-xs text-stone-400 block mb-1">Source</label>
            <select value={sourceFilter} onChange={e => setSourceFilter(e.target.value)}
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
              {sources.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-stone-400 block mb-1">State</label>
            <select value={stateFilter} onChange={e => setStateFilter(e.target.value)}
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
              {states.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-stone-400 block mb-1">Min volume: ${minVolume.toLocaleString()}</label>
            <input type="range" min="0" max="1000000" step="10000" value={minVolume}
              onChange={e => setMinVolume(Number(e.target.value))} className="w-full" />
          </div>
        </div>

        {hasFilters && (
          <button onClick={clearFilters} className="text-xs text-stone-500 hover:text-stone-800 flex items-center gap-1">
            <X className="h-3 w-3" /> Clear filters
          </button>
        )}
      </div>

      {loading ? (
        <div className="grid gap-3">{[1,2,3,4,5].map(i => <div key={i} className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-32"></div>)}</div>
      ) : filtered.length > 0 ? (
        <div className="grid gap-3">
          {filtered.map((g) => {
            const hist = histIndex.get(`${g.race_type}_${g.state}`) || []
            const sourceKeys = Object.keys(g.sources)
            return (
              <Link key={g.race_key} to={`/race/${g.race_key}`}
                className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 hover:border-stone-300 transition-colors block">
                <div className="flex items-start justify-between gap-4 mb-3">
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-stone-800 truncate">{g.title}</div>
                    <div className="flex items-center gap-3 mt-1 text-xs text-stone-400">
                      {g.state && <span className="flex items-center gap-1"><MapPin className="h-3 w-3" />{g.state}</span>}
                      <span className="capitalize">{g.race_type}</span>
                      {g.volume > 0 && <span>${(g.volume / 1000).toFixed(0)}k vol</span>}
                      <span>{sourceKeys.length} source{sourceKeys.length !== 1 ? 's' : ''}</span>
                    </div>
                  </div>
                  <ArrowRight className="h-4 w-4 text-stone-400 shrink-0 mt-1" />
                </div>

                {/* Per-source probability columns, side-by-side */}
                <div className="flex items-stretch gap-2 mb-3">
                  <SourceColumn source="polymarket" market={g.sources.polymarket} />
                  <DeltaBadge polymarket={g.sources.polymarket} kalshi={g.sources.kalshi} />
                  <SourceColumn source="kalshi" market={g.sources.kalshi} />
                  {g.sources.predictit && <SourceColumn source="predictit" market={g.sources.predictit} />}
                </div>

                {/* Historical pattern */}
                {hist.length > 0 && (
                  <div className="flex items-center gap-2 pt-2 border-t border-stone-100">
                    <History className="h-3.5 w-3.5 text-stone-400 shrink-0" />
                    <span className="text-xs text-stone-400">Past:</span>
                    <div className="flex flex-wrap gap-1">
                      {hist.map((h, i) => (
                        <span key={i} className={`text-xs font-medium px-1.5 py-0.5 rounded ${PARTY_COLORS[h.party] || 'bg-stone-100 text-stone-600'}`}
                          title={`${h.winner} (${h.winner_pct}%) beat ${h.runner_up} (${h.runner_up_pct}%) — margin ${h.margin_pct}%`}>
                          {h.year} {h.party}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </Link>
            )
          })}
        </div>
      ) : (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 text-center py-12"><p className="text-stone-400">No races match your filters.</p></div>
      )}
    </div>
  )
}
