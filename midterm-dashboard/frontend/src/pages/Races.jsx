import React, { useState, useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { Search, ArrowRight, MapPin, X, History, Scale, Vote, MessageCircle, GitCompare, Layers } from 'lucide-react'

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

function topProb(market) {
  const outcomes = (market?.outcomes || []).filter(o => typeof o.probability === 'number')
  if (!outcomes.length) return null
  return [...outcomes].sort((a, b) => (b.probability || 0) - (a.probability || 0))[0]
}

function SourceColumn({ source, market, raceTitle, allSources }) {
  const style = SOURCE_STYLES[source] || { bg: 'bg-stone-100', text: 'text-stone-700', bar: '#78716c', label: source }
  const top = market ? topProb(market) : null
  const tradeable = source === 'polymarket' || source === 'kalshi'
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
          {tradeable && (
            <button
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                const poly = allSources?.polymarket
                const kalshi = allSources?.kalshi
                window.hbTrade?.({
                  slug: poly?.slug || market?.slug || '',
                  kalshi_ticker: kalshi?.source_id || market?.source_id || '',
                  token_id: poly?.outcomes?.[0]?.token_id || market?.outcomes?.[0]?.token_id || '',
                  token_id_no: poly?.outcomes?.[1]?.token_id || market?.outcomes?.[1]?.token_id || '',
                  source,
                  question: raceTitle || '',
                  price: top.probability || 0.5,
                  volume: market?.volume || 0,
                })
              }}
              className="mt-2 w-full text-[10px] font-semibold py-1.5 rounded-md transition-all hover:opacity-80"
              style={{ backgroundColor: style.bar + '15', color: style.bar, border: `1px solid ${style.bar}30` }}
            >
              Trade on {style.label}
            </button>
          )}
        </>
      ) : (
        <div className="text-xs text-stone-300 italic py-2">no data</div>
      )}
    </div>
  )
}

function DeltaBadge({ sources }) {
  const probs = Object.entries(sources || {}).map(([src, m]) => {
    const t = topProb(m)
    return t ? { src, prob: t.probability || 0 } : null
  }).filter(Boolean)
  if (probs.length < 2) return null
  const max = Math.max(...probs.map(p => p.prob))
  const min = Math.min(...probs.map(p => p.prob))
  const delta = (max - min) * 100
  if (delta < 0.5) return null
  const color = delta > 10 ? 'bg-red-100 text-red-700' : delta > 5 ? 'bg-amber-100 text-amber-700' : 'bg-emerald-100 text-emerald-700'
  return (
    <div className={`flex flex-col items-center justify-center px-3 py-2 rounded-lg ${color} shrink-0`}>
      <span className="text-[9px] font-bold uppercase tracking-wider opacity-70">Spread</span>
      <span className="text-base font-bold tabular-nums">{delta.toFixed(1)}%</span>
    </div>
  )
}

function HistoryBlock({ hist }) {
  if (!hist?.length) return null
  const last = hist[0]
  const lastColor = last.party === 'D' ? 'border-blue-200 bg-blue-50' : last.party === 'R' ? 'border-red-200 bg-red-50' : 'border-stone-200 bg-stone-50'
  const lastBadge = last.party === 'D' ? 'bg-blue-600 text-white' : last.party === 'R' ? 'bg-red-600 text-white' : 'bg-stone-600 text-white'
  const dWins = hist.filter(h => h.party === 'D').length
  const rWins = hist.filter(h => h.party === 'R').length
  return (
    <div className="pt-3 border-t border-stone-100">
      <div className={`rounded-lg border p-3 ${lastColor}`}>
        <div className="flex items-center justify-between mb-1">
          <div className="flex items-center gap-2">
            <History className="h-3.5 w-3.5 text-stone-500" />
            <span className="text-[10px] text-stone-500 uppercase tracking-wide font-semibold">Last result ({last.year})</span>
          </div>
          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${lastBadge}`}>{last.party} +{last.margin_pct}%</span>
        </div>
        <div className="text-sm font-semibold text-stone-800">{last.winner} <span className="font-normal text-stone-500">beat {last.runner_up}</span></div>
        <div className="text-[11px] text-stone-500">{last.winner_pct}% vs {last.runner_up_pct}% &middot; {last.winner_votes >= 1000000 ? `${(last.winner_votes/1000000).toFixed(1)}M` : `${(last.winner_votes/1000).toFixed(0)}K`} votes</div>
      </div>
      {hist.length > 1 && (
        <div className="flex items-center gap-2 mt-2">
          <span className="text-[10px] text-stone-400">History:</span>
          <div className="flex flex-wrap gap-1">
            {hist.map((h, i) => (
              <span key={i} className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${PARTY_COLORS[h.party] || 'bg-stone-100 text-stone-600'}`}
                title={`${h.winner} (${h.winner_pct}%) beat ${h.runner_up} (${h.runner_up_pct}%)`}>
                {h.year} {h.winner.split(' ').pop()} ({h.party})
              </span>
            ))}
          </div>
          <span className="text-[10px] text-stone-400 ml-auto">
            {dWins > 0 && <span className="text-blue-600 font-semibold">{dWins}D</span>}
            {dWins > 0 && rWins > 0 && ' / '}
            {rWins > 0 && <span className="text-red-600 font-semibold">{rWins}R</span>}
          </span>
        </div>
      )}
    </div>
  )
}

function RaceCard({ g, hist, ctx }) {
  const sourceKeys = Object.keys(g.sources || {})
  const leanColor = ctx?.lean?.includes('D') ? 'bg-blue-100 text-blue-700' : ctx?.lean?.includes('R') ? 'bg-red-100 text-red-700' : ctx?.lean === 'Toss-up' ? 'bg-amber-100 text-amber-700' : ''
  return (
    <Link to={`/race/${g.race_key}`}
      className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 hover:border-stone-300 transition-colors block">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-stone-800 truncate">{g.title}</span>
            {ctx?.lean && <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0 ${leanColor}`}>{ctx.lean}</span>}
          </div>
          <div className="flex items-center gap-3 mt-1 text-xs text-stone-400">
            {g.state && <span className="flex items-center gap-1"><MapPin className="h-3 w-3" />{g.state}</span>}
            <span className="capitalize">{g.race_type}</span>
            {g.volume > 0 && <span>${(g.volume / 1000).toFixed(0)}k vol</span>}
            <span>{sourceKeys.length} source{sourceKeys.length !== 1 ? 's' : ''}</span>
          </div>
        </div>
        <ArrowRight className="h-4 w-4 text-stone-400 shrink-0 mt-1" />
      </div>

      {/* Source columns */}
      <div className="flex items-stretch gap-2 mb-3">
        {sourceKeys.map(src => (
          <SourceColumn key={src} source={src} market={g.sources[src]} raceTitle={g.title} allSources={g.sources} />
        ))}
        {sourceKeys.length >= 2 && <DeltaBadge sources={g.sources} />}
      </div>

      {/* Context */}
      {ctx && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3 pt-3 border-t border-stone-100">
          {ctx.key_issues?.length > 0 && (
            <div>
              <div className="flex items-center gap-1 mb-1">
                <Scale className="h-3 w-3 text-stone-400" />
                <span className="text-[10px] font-semibold text-stone-500 uppercase tracking-wide">Key Issues</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {ctx.key_issues.slice(0, 4).map((iss, i) => (
                  <span key={i} className="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded">{iss}</span>
                ))}
              </div>
            </div>
          )}
          {ctx.referendums?.length > 0 && (
            <div>
              <div className="flex items-center gap-1 mb-1">
                <Vote className="h-3 w-3 text-stone-400" />
                <span className="text-[10px] font-semibold text-stone-500 uppercase tracking-wide">Ballot Measures</span>
              </div>
              {ctx.referendums.map((ref, i) => (
                <div key={i} className="text-[10px] text-stone-600">
                  <span className="font-medium">{ref.title}</span> — {ref.description?.slice(0, 60)}{ref.description?.length > 60 ? '...' : ''}
                </div>
              ))}
            </div>
          )}
          {ctx.public_opinion && (
            <div className="sm:col-span-2">
              <div className="flex items-center gap-1 mb-1">
                <MessageCircle className="h-3 w-3 text-stone-400" />
                <span className="text-[10px] font-semibold text-stone-500 uppercase tracking-wide">Public Sentiment</span>
              </div>
              <div className="text-[10px] text-stone-600">{ctx.public_opinion.top_concern}</div>
            </div>
          )}
          {ctx.incumbents?.length > 0 && (
            <div className="sm:col-span-2">
              <div className="text-[10px] text-stone-500">
                <span className="font-semibold">Incumbent:</span> {ctx.incumbents.map(inc => `${inc.name} (${inc.party}${inc.note ? `, ${inc.note}` : ''})`).join(', ')}
              </div>
            </div>
          )}
        </div>
      )}

      <HistoryBlock hist={hist} />
    </Link>
  )
}

export default function Races() {
  const [matched, setMatched] = useState([])
  const [unmatched, setUnmatched] = useState([])
  const [historical, setHistorical] = useState([])
  const [contexts, setContexts] = useState({})
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [stateFilter, setStateFilter] = useState('all')

  useEffect(() => {
    Promise.all([
      api.races().then(d => {
        setMatched(d?.matched || [])
        setUnmatched(d?.unmatched || [])
      }),
      api.historical().then(d => d?.results || []).catch(() => []),
      api.raceContexts().then(d => d?.contexts || {}).catch(() => ({})),
    ])
      .then(([, h, c]) => { setHistorical(h); setContexts(c) })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const histIndex = useMemo(() => {
    const idx = new Map()
    for (const h of historical) {
      const key = `${h.race_type}_${h.state}`
      if (!idx.has(key)) idx.set(key, [])
      idx.get(key).push(h)
    }
    for (const v of idx.values()) v.sort((a, b) => b.year - a.year)
    return idx
  }, [historical])

  const allRaces = useMemo(() => [...matched, ...unmatched], [matched, unmatched])

  const applyFilters = (list) => list.filter(g => {
    const matchesSearch = !search || (g.title || '').toLowerCase().includes(search.toLowerCase()) || (g.state || '').toLowerCase().includes(search.toLowerCase())
    const matchesType = typeFilter === 'all' || g.race_type === typeFilter
    const matchesState = stateFilter === 'all' || g.state === stateFilter
    return matchesSearch && matchesType && matchesState
  })

  const filteredMatched = useMemo(() => applyFilters(matched), [matched, search, typeFilter, stateFilter])
  const filteredUnmatched = useMemo(() => applyFilters(unmatched), [unmatched, search, typeFilter, stateFilter])

  const types = ['all', ...new Set(allRaces.map(r => r.race_type).filter(Boolean))]
  const states = ['all', ...[...new Set(allRaces.map(r => r.state).filter(Boolean))].sort()]
  const hasFilters = search || typeFilter !== 'all' || stateFilter !== 'all'

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-3xl font-semibold text-stone-800">All Races</h1>
        <span className="text-sm text-stone-400">{filteredMatched.length + filteredUnmatched.length} races</span>
      </div>

      {/* Filters */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 mb-6 space-y-3">
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
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-stone-400 block mb-1">State</label>
            <select value={stateFilter} onChange={e => setStateFilter(e.target.value)}
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
              {states.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>
        {hasFilters && (
          <button onClick={() => { setSearch(''); setTypeFilter('all'); setStateFilter('all') }}
            className="text-xs text-stone-500 hover:text-stone-800 flex items-center gap-1">
            <X className="h-3 w-3" /> Clear filters
          </button>
        )}
      </div>

      {loading ? (
        <div className="grid gap-3">{[1,2,3,4,5].map(i => <div key={i} className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-32"></div>)}</div>
      ) : (
        <>
          {/* SECTION 1: Cross-Source Comparison */}
          <div className="mb-8">
            <div className="flex items-center gap-2 mb-4">
              <GitCompare className="h-5 w-5 text-emerald-600" />
              <h2 className="text-xl font-semibold text-stone-800">Cross-Source Comparison</h2>
              <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full font-medium">{filteredMatched.length} races</span>
            </div>
            <p className="text-xs text-stone-400 mb-4">Elections tracked by multiple prediction markets. Compare odds across Polymarket, Kalshi, and PredictIt.</p>
            {filteredMatched.length > 0 ? (
              <div className="grid gap-3">
                {filteredMatched.map(g => (
                  <RaceCard key={g.race_key} g={g}
                    hist={histIndex.get(`${g.race_type}_${g.state}`) || []}
                    ctx={contexts[`${g.race_type}_${g.state}`]} />
                ))}
              </div>
            ) : (
              <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 text-center">
                <p className="text-stone-400">No cross-source races match your filters.</p>
              </div>
            )}
          </div>

          {/* SECTION 2: Single Source Markets */}
          <div>
            <div className="flex items-center gap-2 mb-4">
              <Layers className="h-5 w-5 text-stone-500" />
              <h2 className="text-xl font-semibold text-stone-800">Single Source Markets</h2>
              <span className="text-xs bg-stone-100 text-stone-600 px-2 py-0.5 rounded-full font-medium">{filteredUnmatched.length} races</span>
            </div>
            <p className="text-xs text-stone-400 mb-4">Elections available on only one platform. Historical data shows past winners and partisan lean.</p>
            {filteredUnmatched.length > 0 ? (
              <div className="grid gap-3">
                {filteredUnmatched.map(g => (
                  <RaceCard key={g.race_key} g={g}
                    hist={histIndex.get(`${g.race_type}_${g.state}`) || []}
                    ctx={contexts[`${g.race_type}_${g.state}`]} />
                ))}
              </div>
            ) : (
              <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 text-center">
                <p className="text-stone-400">No single-source races match your filters.</p>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
