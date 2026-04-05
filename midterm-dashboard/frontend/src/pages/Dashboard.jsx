import React, { useState, useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { AlertTriangle, ArrowRight, BarChart3, Newspaper, TrendingUp } from 'lucide-react'

const SOURCE_COLORS = {
  polymarket: { bg: 'bg-violet-100', text: 'text-violet-700', dot: 'bg-violet-500', hex: '#8b5cf6' },
  kalshi: { bg: 'bg-blue-100', text: 'text-blue-700', dot: 'bg-blue-500', hex: '#3b82f6' },
  predictit: { bg: 'bg-emerald-100', text: 'text-emerald-700', dot: 'bg-emerald-500', hex: '#10b981' },
  polling: { bg: 'bg-amber-100', text: 'text-amber-700', dot: 'bg-amber-500', hex: '#f59e0b' },
}

function getSourceStyle(source) {
  return SOURCE_COLORS[source?.toLowerCase()] || { bg: 'bg-stone-100', text: 'text-stone-600', dot: 'bg-stone-400', hex: '#78716c' }
}

const PARTY_COLORS = { DEM: 'text-blue-600', REP: 'text-rose-500', IND: 'text-amber-600' }
function partyColor(party) {
  if (!party) return 'text-stone-600'
  const p = party.toUpperCase()
  if (p.startsWith('DEM') || p === 'D') return PARTY_COLORS.DEM
  if (p.startsWith('REP') || p === 'R') return PARTY_COLORS.REP
  if (p.startsWith('IND') || p === 'I') return PARTY_COLORS.IND
  return 'text-stone-600'
}

function ControlCardCompact({ title, data }) {
  if (!data) return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 animate-pulse">
      <div className="h-4 bg-stone-100 rounded w-1/2 mb-3"></div>
      <div className="h-8 bg-stone-100 rounded"></div>
    </div>
  )

  const sources = data.sources || {}
  const sourceCount = Math.max(Object.keys(sources).length, 1)
  const avgDem = Object.values(sources).reduce((s, v) => s + (v.democrat || 0), 0) / sourceCount
  const avgRep = Object.values(sources).reduce((s, v) => s + (v.republican || 0), 0) / sourceCount
  const leader = avgDem >= avgRep ? 'D' : 'R'
  const leaderPct = Math.max(avgDem, avgRep) * 100

  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-stone-800">{title}</h3>
        <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${leader === 'D' ? 'bg-blue-50 text-blue-600' : 'bg-rose-50 text-rose-500'}`}>
          {leader === 'D' ? 'Dem' : 'Rep'} {leaderPct.toFixed(0)}%
        </span>
      </div>
      <div className="flex gap-1 h-2.5 rounded-full overflow-hidden bg-stone-100">
        <div className="bg-blue-500 rounded-l-full transition-all" style={{ width: `${avgDem * 100}%` }}></div>
        <div className="bg-rose-500 rounded-r-full transition-all" style={{ width: `${avgRep * 100}%` }}></div>
      </div>
      <div className="flex justify-between mt-2 text-xs text-stone-500">
        <span className="text-blue-600 font-medium">D {(avgDem * 100).toFixed(1)}%</span>
        <span className="text-rose-500 font-medium">R {(avgRep * 100).toFixed(1)}%</span>
      </div>
      {Object.keys(sources).length > 1 && (
        <div className="flex gap-2 mt-2 flex-wrap">
          {Object.entries(sources).map(([source, vals]) => {
            const style = getSourceStyle(source)
            return (
              <span key={source} className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full ${style.bg} ${style.text}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`}></span>
                {source} {((vals.democrat || 0) * 100).toFixed(0)}/{((vals.republican || 0) * 100).toFixed(0)}
              </span>
            )
          })}
        </div>
      )}
    </div>
  )
}

function SourceMarketSection({ title, sourceKey, markets, color }) {
  const sourceMarkets = markets.filter(m => m.source === sourceKey)
  if (sourceMarkets.length === 0) return null

  const style = getSourceStyle(sourceKey)

  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-stone-800 flex items-center gap-2">
          <span className={`w-2.5 h-2.5 rounded-full ${style.dot}`}></span>
          {title}
          <span className="text-xs font-normal text-stone-400">{sourceMarkets.length} markets</span>
        </h3>
        <Link to="/races" className="text-stone-900 text-xs hover:underline">View all</Link>
      </div>
      <div className="divide-y divide-stone-50">
        {sourceMarkets.slice(0, 8).map((m, i) => {
          const outcomes = m.outcomes || []
          const topOutcome = outcomes.length > 0
            ? outcomes.reduce((a, b) => ((b.probability || 0) > (a.probability || 0) ? b : a), outcomes[0])
            : null
          const prob = topOutcome ? (topOutcome.probability || 0) * 100 : 0
          const raceKey = m.race_key || `${m.race_type || 'other'}_${m.state || 'US'}_${m.source_id || ''}`

          return (
            <Link key={i} to={`/race/${raceKey}`} className="flex items-center justify-between py-2.5 px-2 hover:bg-stone-50 rounded-lg transition-colors">
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-stone-800 truncate">{m.title || m.event_title}</div>
                <div className="flex items-center gap-2 mt-0.5">
                  {m.state && <span className="text-[10px] text-stone-400 uppercase">{m.state}</span>}
                  <span className="text-[10px] text-stone-400 capitalize">{m.race_type}</span>
                  {m.volume > 0 && <span className="text-[10px] text-stone-300">${(m.volume / 1000).toFixed(0)}k</span>}
                </div>
              </div>
              <div className="flex items-center gap-3 flex-shrink-0">
                {topOutcome && (
                  <div className="text-right">
                    <div className="text-sm font-bold text-stone-800 tabular-nums">{prob.toFixed(0)}%</div>
                    <div className="text-[10px] text-stone-400 truncate max-w-[80px]">{topOutcome.name}</div>
                  </div>
                )}
                <ArrowRight className="h-3 w-3 text-stone-300" />
              </div>
            </Link>
          )
        })}
      </div>
      {sourceMarkets.length > 8 && (
        <div className="text-center pt-2 mt-2 border-t border-stone-50">
          <Link to="/races" className="text-xs text-stone-500 hover:text-stone-800">+{sourceMarkets.length - 8} more markets</Link>
        </div>
      )}
    </div>
  )
}

function PollsSection({ polls, loading }) {
  if (loading) return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
      <div className="flex items-center gap-2 mb-3">
        <Newspaper className="h-4 w-4 text-stone-600" />
        <h3 className="text-sm font-semibold text-stone-800">Latest Polls</h3>
      </div>
      <div className="space-y-2">{[1,2,3,4].map(i => <div key={i} className="h-10 bg-stone-100 rounded animate-pulse"></div>)}</div>
    </div>
  )

  // Group polls by pollster + state + poll_type to show one row per poll
  const grouped = {}
  polls.forEach(p => {
    const key = `${p.pollster}_${p.state || 'US'}_${p.poll_type}_${p.end_date}`
    if (!grouped[key]) {
      grouped[key] = { pollster: p.pollster, state: p.state, poll_type: p.poll_type, end_date: p.end_date, sample_size: p.sample_size, candidates: [] }
    }
    grouped[key].candidates.push({ name: p.candidate, party: p.party, pct: p.percentage })
  })
  const pollRows = Object.values(grouped).slice(0, 12)

  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-stone-800 flex items-center gap-2">
          <Newspaper className="h-4 w-4 text-amber-600" />
          Latest Polls
          <span className="text-xs font-normal text-stone-400">538</span>
        </h3>
      </div>
      {pollRows.length > 0 ? (
        <div className="divide-y divide-stone-50">
          {pollRows.map((row, i) => {
            // Sort candidates by percentage desc
            const sorted = [...row.candidates].sort((a, b) => (b.pct || 0) - (a.pct || 0))
            return (
              <div key={i} className="py-2.5 px-2">
                <div className="flex items-center justify-between mb-1.5">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-xs font-medium text-stone-800 truncate">{row.pollster || 'Unknown'}</span>
                    {row.state && <span className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-500 uppercase flex-shrink-0">{row.state}</span>}
                    <span className="text-[10px] text-stone-400 capitalize flex-shrink-0">{row.poll_type}</span>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {row.sample_size && <span className="text-[10px] text-stone-300">n={row.sample_size}</span>}
                    {row.end_date && <span className="text-[10px] text-stone-300">{row.end_date}</span>}
                  </div>
                </div>
                <div className="flex gap-3">
                  {sorted.slice(0, 3).map((c, j) => (
                    <span key={j} className="text-xs">
                      <span className={`font-bold tabular-nums ${partyColor(c.party)}`}>{c.pct?.toFixed(1) || '—'}%</span>
                      <span className="text-stone-400 ml-1">{c.name || c.party}</span>
                    </span>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        <p className="text-stone-400 text-xs py-4 text-center">No polling data yet. Data refreshes every 5 minutes.</p>
      )}
    </div>
  )
}

export default function Dashboard() {
  const [overview, setOverview] = useState(null)
  const [divergences, setDivergences] = useState([])
  const [races, setRaces] = useState([])
  const [polls, setPolls] = useState([])
  const [loading, setLoading] = useState(true)
  const [pollsLoading, setPollsLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      api.overview().catch(() => null),
      api.divergence().catch(() => []),
      api.races().catch(() => []),
    ]).then(([ov, div, rc]) => {
      setOverview(ov)
      setDivergences(Array.isArray(div) ? div.slice(0, 8) : (div?.divergences || []).slice(0, 8))
      setRaces(Array.isArray(rc) ? rc : (rc?.races || []))
    }).finally(() => setLoading(false))

    api.recentPolls().then(data => setPolls(data?.polls || []))
      .catch(() => {}).finally(() => setPollsLoading(false))
  }, [])

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-3xl font-semibold text-stone-800 mb-1">2026 Midterms</h1>
        <p className="text-stone-500 text-sm">Real-time prediction market odds, polling data, and source divergence analysis.</p>
      </div>

      {/* Compact control cards */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <ControlCardCompact title="Senate Control" data={overview?.senate_control} />
        <ControlCardCompact title="House Control" data={overview?.house_control} />
      </div>

      {/* Source-specific market sections */}
      <div className="grid lg:grid-cols-2 gap-6 mb-6">
        <SourceMarketSection title="Polymarket" sourceKey="polymarket" markets={races} />
        <SourceMarketSection title="Kalshi" sourceKey="kalshi" markets={races} />
      </div>

      {/* PredictIt if present */}
      {races.some(r => r.source === 'predictit') && (
        <div className="grid lg:grid-cols-2 gap-6 mb-6">
          <SourceMarketSection title="PredictIt" sourceKey="predictit" markets={races} />
          <div></div>
        </div>
      )}

      {/* Polls + Divergences */}
      <div className="grid lg:grid-cols-2 gap-6 mb-6">
        <PollsSection polls={polls} loading={pollsLoading} />

        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-stone-800 flex items-center gap-2">
              <AlertTriangle className="h-4 w-4 text-amber-600" />Top Divergences
            </h3>
            <Link to="/divergence" className="text-stone-900 text-xs hover:underline">View all</Link>
          </div>
          {loading ? (
            <div className="space-y-2">{[1,2,3].map(i => <div key={i} className="h-8 bg-stone-100 rounded animate-pulse"></div>)}</div>
          ) : divergences.length > 0 ? (
            <div className="divide-y divide-stone-50">
              {divergences.map((d, i) => {
                const maxDiv = (d.max_divergence || 0) * 100
                const color = maxDiv > 15 ? 'text-rose-500' : maxDiv > 8 ? 'text-amber-600' : 'text-emerald-600'
                return (
                  <Link key={i} to={`/race/${d.race_key}`} className="flex items-center justify-between py-2 px-2 hover:bg-stone-50 rounded-lg transition-colors">
                    <div className="min-w-0">
                      <div className="font-medium text-xs text-stone-800 truncate">{d.race_key?.replace('_', ' - ')}</div>
                      <div className="text-[10px] text-stone-400">{d.state} <span className="capitalize">{d.race_type}</span></div>
                    </div>
                    <div className="flex items-center gap-2 flex-shrink-0">
                      <span className={`font-bold text-xs tabular-nums ${color}`}>{maxDiv.toFixed(1)}%</span>
                      <ArrowRight className="h-3 w-3 text-stone-300" />
                    </div>
                  </Link>
                )
              })}
            </div>
          ) : <p className="text-stone-400 text-xs">No divergence data yet.</p>}
        </div>
      </div>

      {/* All Markets Overview */}
      {races.length > 0 && (
        <div className="mb-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-stone-800 flex items-center gap-2">
              <TrendingUp className="h-5 w-5 text-stone-600" />
              All Markets
              <span className="text-sm font-normal text-stone-400">{races.length} total</span>
            </h2>
            <Link to="/races" className="text-stone-900 text-sm hover:underline">Browse all</Link>
          </div>
          <div className="bg-white shadow-sm border border-stone-100 rounded-xl overflow-hidden">
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-stone-100">
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5">Market</th>
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5">Source</th>
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5">State</th>
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5">Type</th>
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5 text-right">Top Odds</th>
                  <th className="text-[10px] font-medium text-stone-400 uppercase tracking-wide px-4 py-2.5 text-right">Volume</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-stone-50">
                {races.slice(0, 20).map((m, i) => {
                  const outcomes = m.outcomes || []
                  const topOutcome = outcomes.length > 0
                    ? outcomes.reduce((a, b) => ((b.probability || 0) > (a.probability || 0) ? b : a), outcomes[0])
                    : null
                  const prob = topOutcome ? (topOutcome.probability || 0) * 100 : 0
                  const style = getSourceStyle(m.source)
                  const raceKey = m.race_key || `${m.race_type || 'other'}_${m.state || 'US'}_${m.source_id || ''}`

                  return (
                    <tr key={i} className="hover:bg-stone-50 transition-colors cursor-pointer group">
                      <td className="px-4 py-2.5">
                        <Link to={`/race/${raceKey}`} className="text-xs font-medium text-stone-800 group-hover:text-stone-900 line-clamp-1">
                          {m.title || m.event_title}
                        </Link>
                      </td>
                      <td className="px-4 py-2.5">
                        <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full ${style.bg} ${style.text}`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`}></span>
                          {m.source}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-[10px] text-stone-500 uppercase">{m.state || '—'}</td>
                      <td className="px-4 py-2.5 text-[10px] text-stone-500 capitalize">{m.race_type || '—'}</td>
                      <td className="px-4 py-2.5 text-right">
                        {topOutcome ? (
                          <span className="text-xs font-bold text-stone-800 tabular-nums">{prob.toFixed(0)}%</span>
                        ) : <span className="text-xs text-stone-300">—</span>}
                      </td>
                      <td className="px-4 py-2.5 text-right text-[10px] text-stone-400">
                        {m.volume > 0 ? `$${(m.volume / 1000).toFixed(0)}k` : '—'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {races.length > 20 && (
              <div className="text-center py-3 border-t border-stone-50">
                <Link to="/races" className="text-xs text-stone-500 hover:text-stone-800">View all {races.length} markets</Link>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
