import React, { useState, useEffect, useMemo } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../lib/api'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend, CartesianGrid, BarChart, Bar, Cell } from 'recharts'
import { ArrowLeft, Clock, Eye, EyeOff, TrendingUp, BarChart3, History, Trophy } from 'lucide-react'

const sourceColors = { polymarket: '#8b5cf6', kalshi: '#3b82f6', predictit: '#f59e0b', polling: '#10b981', metaculus: '#a855f7' }
const sourceLabels = { polymarket: 'Polymarket', kalshi: 'Kalshi', predictit: 'PredictIt', polling: '538 Polling', metaculus: 'Metaculus' }

const PARTY_COLORS = { DEM: '#3b82f6', REP: '#ef4444', IND: '#f59e0b' }
function partyColor(party) {
  if (!party) return '#78716c'
  const p = party.toUpperCase()
  if (p.startsWith('DEM') || p === 'D') return PARTY_COLORS.DEM
  if (p.startsWith('REP') || p === 'R') return PARTY_COLORS.REP
  return '#78716c'
}

function OutcomeBar({ name, probability, maxProb, color }) {
  const pct = (probability || 0) * 100
  const width = maxProb > 0 ? (probability / maxProb) * 100 : 0
  return (
    <div className="flex items-center gap-3 py-1">
      <span className="text-xs text-stone-600 w-28 truncate flex-shrink-0" title={name}>{name}</span>
      <div className="flex-1 h-5 bg-stone-100 rounded overflow-hidden relative">
        <div className="h-full rounded transition-all" style={{ width: `${width}%`, backgroundColor: color || '#78716c' }}></div>
      </div>
      <span className="text-xs font-bold text-stone-800 tabular-nums w-12 text-right">{pct.toFixed(1)}%</span>
    </div>
  )
}

export default function RaceDetail() {
  const { raceKey } = useParams()
  const [race, setRace] = useState(null)
  const [history, setHistory] = useState([])
  const [polls, setPolls] = useState([])
  const [historicalResults, setHistoricalResults] = useState([])
  const [raceContext, setRaceContext] = useState(null)
  const [loading, setLoading] = useState(true)
  const [visibleSources, setVisibleSources] = useState(new Set(Object.keys(sourceColors)))

  useEffect(() => {
    Promise.all([
      api.race(raceKey).catch(() => null),
      api.history(raceKey).catch(() => []),
      api.polling(raceKey).catch(() => []),
      api.raceContext(raceKey).catch(() => null),
    ]).then(([r, h, p, ctx]) => {
      setRace(r)
      setHistory(Array.isArray(h) ? h : h?.history || [])
      setPolls(Array.isArray(p) ? p : p?.polls || [])
      if (ctx && ctx.found !== false) setRaceContext(ctx)
      // Also try fetching history by the canonical race_key if returned
      if (r?.race_key && r.race_key !== raceKey) {
        api.history(r.race_key).then(h2 => {
          const hist = Array.isArray(h2) ? h2 : h2?.history || []
          if (hist.length > 0) setHistory(hist)
        }).catch(() => {})
        // Also fetch context by canonical key
        if (!ctx || ctx.found === false) {
          api.raceContext(r.race_key).then(c => { if (c && c.found !== false) setRaceContext(c) }).catch(() => {})
        }
      }
      // Fetch historical election results for this race_type + state
      if (r?.race_type && r?.state) {
        api.historical({ race_type: r.race_type, state: r.state })
          .then(d => setHistoricalResults(d?.results || []))
          .catch(() => {})
      }
    }).finally(() => setLoading(false))
  }, [raceKey])

  const toggleSource = (source) => {
    setVisibleSources(prev => {
      const next = new Set(prev)
      next.has(source) ? next.delete(source) : next.add(source)
      return next
    })
  }

  const maxSpread = useMemo(() => {
    const sources = race?.by_source || {}
    const sourceKeys = Object.keys(sources)
    if (sourceKeys.length < 2) return null

    let maxDiff = 0
    let pair = ['', '']
    for (let i = 0; i < sourceKeys.length; i++) {
      for (let j = i + 1; j < sourceKeys.length; j++) {
        const o1 = sources[sourceKeys[i]]?.outcomes || []
        const o2 = sources[sourceKeys[j]]?.outcomes || []
        if (o1.length > 0 && o2.length > 0) {
          const diff = Math.abs((o1[0]?.probability || 0) - (o2[0]?.probability || 0))
          if (diff > maxDiff) { maxDiff = diff; pair = [sourceKeys[i], sourceKeys[j]] }
        }
      }
    }
    return maxDiff > 0 ? { spread: maxDiff, sources: pair } : null
  }, [race])

  const spreadColor = maxSpread
    ? maxSpread.spread > 0.1 ? 'bg-red-100 text-red-700' : maxSpread.spread > 0.05 ? 'bg-amber-100 text-amber-700' : 'bg-emerald-100 text-emerald-700'
    : ''

  const availableSources = useMemo(() => {
    if (history.length === 0) return []
    return Object.keys(sourceColors).filter(src => history.some(d => d[src] != null))
  }, [history])

  if (loading) return (
    <div className="space-y-6">
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-32"></div>
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-64"></div>
    </div>
  )

  if (!race) return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 text-center py-12">
      <p className="text-stone-400 mb-4">Race not found</p>
      <Link to="/races" className="btn-primary">Back to Races</Link>
    </div>
  )

  const sourcesArr = Object.entries(race.by_source || {})
  const sourceCount = sourcesArr.length

  return (
    <div>
      <Link to="/races" className="flex items-center gap-1 text-stone-500 hover:text-stone-700 text-sm mb-4">
        <ArrowLeft className="h-4 w-4" /> Back to Races
      </Link>

      {/* Header */}
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
        <div className="flex items-start justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-2xl font-semibold text-stone-800">{race.title || race.event_title}</h1>
            <div className="flex items-center gap-3 mt-1 text-sm text-stone-500">
              {race.state && <span className="uppercase font-medium">{race.state}</span>}
              <span className="capitalize">{race.race_type}</span>
              <span className="text-stone-300">{sourceCount} source{sourceCount !== 1 ? 's' : ''}</span>
            </div>
          </div>
          {maxSpread && (
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium ${spreadColor}`}>
              <TrendingUp className="h-3 w-3" />
              {(maxSpread.spread * 100).toFixed(1)}% spread
              <span className="opacity-70">({sourceLabels[maxSpread.sources[0]] || maxSpread.sources[0]} vs {sourceLabels[maxSpread.sources[1]] || maxSpread.sources[1]})</span>
            </div>
          )}
        </div>

        {/* Source Comparison with outcome bars */}
        <h3 className="text-sm font-semibold text-stone-500 mb-3 mt-6 uppercase tracking-wide">Source Comparison</h3>
        <div className={`grid gap-4 ${sourceCount === 1 ? 'grid-cols-1 max-w-xl' : sourceCount <= 2 ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1 md:grid-cols-2 lg:grid-cols-3'}`}>
          {sourcesArr.map(([source, data]) => {
            const outcomes = data.outcomes || []
            const maxProb = Math.max(...outcomes.map(o => o.probability || 0), 0.01)
            const color = sourceColors[source] || '#78716c'
            const tradeable = source === 'polymarket' || source === 'kalshi'
            const topOutcome = outcomes.length > 0 ? outcomes.reduce((a, b) => ((b.probability || 0) > (a.probability || 0) ? b : a), outcomes[0]) : null

            return (
              <div key={source} className="bg-stone-50 rounded-lg p-4">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-xs font-medium text-stone-600 flex items-center gap-1.5">
                    <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }}></div>
                    {sourceLabels[source] || source}
                  </div>
                  <div className="flex items-center gap-2">
                    {data.volume > 0 && (
                      <span className="text-[10px] text-stone-400">${(data.volume / 1000).toFixed(0)}k vol</span>
                    )}
                    {tradeable && (
                      <button
                        onClick={() => {
                          const polyData = race.by_source?.polymarket
                          const kalshiData = race.by_source?.kalshi
                          window.hbTrade?.({
                            slug: polyData?.slug || data.slug || '',
                            kalshi_ticker: kalshiData?.source_id || '',
                            token_id: polyData?.outcomes?.[0]?.token_id || '',
                            token_id_no: polyData?.outcomes?.[1]?.token_id || '',
                            source: source,
                            question: race.title || race.event_title || '',
                            price: topOutcome?.probability || 0.5,
                            volume: data.volume || 0,
                          })
                        }}
                        className="text-[10px] font-semibold px-2.5 py-1 rounded-md transition-all hover:scale-105"
                        style={{ backgroundColor: color + '18', color: color, border: `1px solid ${color}40` }}
                      >
                        Trade
                      </button>
                    )}
                  </div>
                </div>
                {data.title && data.title !== race.title && (
                  <p className="text-[10px] text-stone-400 mb-2 italic">{data.title}</p>
                )}
                <div className="space-y-0.5">
                  {outcomes.slice(0, 12).map((o, i) => (
                    <OutcomeBar key={i} name={o.name} probability={o.probability} maxProb={maxProb} color={color} />
                  ))}
                  {outcomes.length > 12 && (
                    <p className="text-[10px] text-stone-400 pt-1">+{outcomes.length - 12} more outcomes</p>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* Side-by-side comparison chart when multiple sources */}
        {sourceCount > 1 && (() => {
          // Build comparison data: for the top outcome name, show each source's probability
          const compData = sourcesArr.map(([source, data]) => {
            const outcomes = data.outcomes || []
            const top = outcomes.length > 0 ? outcomes.reduce((a, b) => ((b.probability || 0) > (a.probability || 0) ? b : a), outcomes[0]) : null
            return {
              source: sourceLabels[source] || source,
              probability: top ? (top.probability || 0) * 100 : 0,
              color: sourceColors[source] || '#78716c',
            }
          })

          return (
            <div className="mt-6">
              <h3 className="text-sm font-semibold text-stone-500 mb-3 uppercase tracking-wide">Lead Outcome Across Sources</h3>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={compData} layout="vertical" margin={{ left: 80 }}>
                  <XAxis type="number" domain={[0, 100]} tick={{ fill: '#78716c', fontSize: 12 }} tickFormatter={v => `${v}%`} />
                  <YAxis type="category" dataKey="source" tick={{ fill: '#78716c', fontSize: 12 }} width={80} />
                  <Tooltip contentStyle={{ backgroundColor: '#fff', border: '1px solid #e7e5e4', borderRadius: '12px', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.05)' }} formatter={v => `${v.toFixed(1)}%`} />
                  <Bar dataKey="probability" radius={[0, 4, 4, 0]}>
                    {compData.map((d, i) => <Cell key={i} fill={d.color} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          )
        })()}
      </div>

      {/* Race Context: Policies, Referendums, Public Opinion */}
      {raceContext && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-stone-800">Race Context</h3>
            {raceContext.lean && (
              <span className={`text-xs font-bold px-2.5 py-1 rounded-full ${
                raceContext.lean.includes('D') ? 'bg-blue-100 text-blue-700' :
                raceContext.lean.includes('R') ? 'bg-red-100 text-red-700' :
                'bg-amber-100 text-amber-700'
              }`}>{raceContext.lean}</span>
            )}
          </div>

          {raceContext.context && (
            <p className="text-sm text-stone-600 mb-4 leading-relaxed">{raceContext.context}</p>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {/* Candidates & Policies */}
            {raceContext.candidates?.length > 0 && (
              <div className="bg-stone-50 rounded-lg p-4">
                <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                  <TrendingUp className="h-3.5 w-3.5" /> Candidates & Policies
                </h4>
                {raceContext.candidates.map((c, i) => (
                  <div key={i} className="mb-3 last:mb-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                        c.party === 'D' ? 'bg-blue-600 text-white' : c.party === 'R' ? 'bg-red-600 text-white' : 'bg-stone-600 text-white'
                      }`}>{c.party}</span>
                      <span className="text-sm font-semibold text-stone-800">{c.name}</span>
                      {c.status && <span className="text-[10px] text-stone-400 capitalize">({c.status})</span>}
                    </div>
                    {c.policies && (
                      <ul className="space-y-0.5 ml-1">
                        {c.policies.map((p, j) => (
                          <li key={j} className="text-xs text-stone-600 flex items-start gap-1.5">
                            <span className="text-stone-300 mt-0.5">&#x2022;</span> {p}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* Key Issues + Public Opinion */}
            <div className="space-y-4">
              {raceContext.key_issues?.length > 0 && (
                <div className="bg-stone-50 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                    Key Issues
                  </h4>
                  <div className="flex flex-wrap gap-1.5">
                    {raceContext.key_issues.map((iss, i) => (
                      <span key={i} className="text-xs bg-white border border-stone-200 text-stone-700 px-2 py-1 rounded-md">{iss}</span>
                    ))}
                  </div>
                </div>
              )}

              {raceContext.public_opinion && (
                <div className="bg-stone-50 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                    Public Sentiment
                  </h4>
                  {raceContext.public_opinion.approval && (
                    <p className="text-xs text-stone-600 mb-1">{raceContext.public_opinion.approval}</p>
                  )}
                  {raceContext.public_opinion.top_concern && (
                    <p className="text-xs text-stone-700 font-medium">{raceContext.public_opinion.top_concern}</p>
                  )}
                </div>
              )}

              {/* Referendums / Ballot Measures */}
              {raceContext.referendums?.length > 0 && (
                <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-amber-700 uppercase tracking-wide mb-2">
                    Ballot Measures
                  </h4>
                  {raceContext.referendums.map((ref, i) => (
                    <div key={i} className="mb-2 last:mb-0">
                      <div className="text-xs font-semibold text-stone-800">{ref.title}</div>
                      <div className="text-[10px] text-amber-700 font-medium">{ref.topic}</div>
                      <div className="text-xs text-stone-600 mt-0.5">{ref.description}</div>
                    </div>
                  ))}
                </div>
              )}

              {/* Incumbent info */}
              {raceContext.incumbents?.length > 0 && (
                <div className="bg-stone-50 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2">Incumbent</h4>
                  {raceContext.incumbents.map((inc, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                        inc.party === 'D' ? 'bg-blue-100 text-blue-700' : inc.party === 'R' ? 'bg-red-100 text-red-700' : 'bg-stone-100 text-stone-700'
                      }`}>{inc.party}</span>
                      <span className="text-sm text-stone-800 font-medium">{inc.name}</span>
                      <span className="text-xs text-stone-400">since {inc.since}</span>
                      {inc.note && <span className="text-[10px] text-amber-600 font-medium">({inc.note})</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Historical Results */}
      {historicalResults.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <h3 className="text-lg font-semibold text-stone-800 mb-1 flex items-center gap-2">
            <History className="h-5 w-5 text-amber-600" />
            Historical Results — {race.state} {race.race_type}
          </h3>
          <p className="text-xs text-stone-400 mb-4">Past winners for this seat. Use this to spot partisan patterns vs current market odds.</p>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {historicalResults.map((h, i) => {
              const winColor = h.party === 'D' ? 'bg-blue-50 border-blue-200' : h.party === 'R' ? 'bg-red-50 border-red-200' : 'bg-stone-50 border-stone-200'
              const winBadge = h.party === 'D' ? 'bg-blue-600 text-white' : h.party === 'R' ? 'bg-red-600 text-white' : 'bg-stone-600 text-white'
              return (
                <div key={i} className={`border rounded-lg p-3 ${winColor}`}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-bold text-stone-500">{h.year}</span>
                    <div className="flex items-center gap-1">
                      <Trophy className="h-3 w-3 text-amber-500" />
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${winBadge}`}>{h.party}</span>
                    </div>
                  </div>
                  <div className="text-sm font-semibold text-stone-800">{h.winner}</div>
                  <div className="text-xs text-stone-500">{h.winner_pct}% &middot; {(h.winner_votes / 1000).toFixed(0)}k votes</div>
                  <div className="border-t border-stone-200 mt-2 pt-2 text-xs text-stone-500">
                    beat {h.runner_up} ({h.runner_up_party}, {h.runner_up_pct}%)
                  </div>
                  <div className="text-[10px] text-stone-400 mt-1">Margin: {h.margin_pct}%</div>
                </div>
              )
            })}
          </div>
          {(() => {
            const dWins = historicalResults.filter(h => h.party === 'D').length
            const rWins = historicalResults.filter(h => h.party === 'R').length
            return (
              <div className="mt-4 pt-4 border-t border-stone-100 flex items-center gap-4 text-sm">
                <span className="text-stone-500">Pattern ({historicalResults.length} elections):</span>
                {dWins > 0 && <span className="text-blue-700 font-semibold">{dWins} D win{dWins !== 1 ? 's' : ''}</span>}
                {rWins > 0 && <span className="text-red-700 font-semibold">{rWins} R win{rWins !== 1 ? 's' : ''}</span>}
              </div>
            )
          })()}
        </div>
      )}

      {/* Price History */}
      {history.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <h3 className="text-lg font-semibold text-stone-800 flex items-center gap-2">
              <Clock className="h-5 w-5 text-stone-500" />Price History
            </h3>
            <div className="flex gap-1.5 flex-wrap">
              {availableSources.map(source => (
                <button key={source} onClick={() => toggleSource(source)}
                  className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-all border ${
                    visibleSources.has(source) ? 'border-stone-200 bg-white text-stone-700 shadow-sm' : 'border-transparent bg-stone-100 text-stone-400'
                  }`}>
                  {visibleSources.has(source) ? <Eye className="h-3 w-3" /> : <EyeOff className="h-3 w-3" />}
                  <div className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: sourceColors[source] }}></div>
                  {sourceLabels[source] || source}
                </button>
              ))}
            </div>
          </div>
          <ResponsiveContainer width="100%" height={350}>
            <LineChart data={history}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e7e5e4" />
              <XAxis dataKey="date" tick={{ fill: '#78716c', fontSize: 12 }} />
              <YAxis tick={{ fill: '#78716c', fontSize: 12 }} domain={[0, 1]} tickFormatter={v => `${(v * 100).toFixed(0)}%`} />
              <Tooltip
                contentStyle={{ backgroundColor: '#fff', border: '1px solid #e7e5e4', borderRadius: '12px', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.05)' }}
                formatter={v => `${(v * 100).toFixed(1)}%`}
              />
              <Legend />
              {Object.keys(sourceColors).filter(s => visibleSources.has(s)).map(source => (
                <Line key={source} type="monotone" dataKey={source} name={sourceLabels[source] || source}
                  stroke={sourceColors[source]} strokeWidth={source === 'polling' ? 2.5 : 2}
                  strokeDasharray={source === 'polling' ? '6 3' : undefined} dot={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Polls */}
      {polls.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
          <h3 className="text-lg font-semibold text-stone-800 mb-4 flex items-center gap-2">
            <BarChart3 className="h-5 w-5 text-amber-600" />
            538 Polling Data
            <span className="text-xs font-normal text-stone-400">{polls.length} polls</span>
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-stone-100">
                  <th className="text-left py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">Pollster</th>
                  <th className="text-left py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">Candidate</th>
                  <th className="text-left py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">Party</th>
                  <th className="text-right py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">%</th>
                  <th className="text-right py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">Sample</th>
                  <th className="text-right py-2.5 px-3 text-[10px] font-semibold text-stone-400 uppercase tracking-wide">Date</th>
                </tr>
              </thead>
              <tbody>
                {polls.slice(0, 30).map((poll, i) => (
                  <tr key={i} className="border-b border-stone-50 hover:bg-stone-50 transition-colors">
                    <td className="py-2 px-3 text-xs text-stone-700 font-medium">{poll.pollster || '—'}</td>
                    <td className="py-2 px-3 text-xs text-stone-600">{poll.candidate || '—'}</td>
                    <td className="py-2 px-3">
                      <span className="text-xs font-bold" style={{ color: partyColor(poll.party) }}>{poll.party || '—'}</span>
                    </td>
                    <td className="py-2 px-3 text-right">
                      <span className="text-xs font-bold text-stone-800">{poll.percentage != null ? `${poll.percentage}%` : '—'}</span>
                    </td>
                    <td className="py-2 px-3 text-right text-xs text-stone-400">{poll.sample_size != null ? poll.sample_size.toLocaleString() : '—'}</td>
                    <td className="py-2 px-3 text-right text-xs text-stone-400">{poll.end_date || poll.start_date || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {polls.length > 30 && <p className="text-xs text-stone-400 text-center py-2">Showing 30 of {polls.length} polls</p>}
          </div>
        </div>
      )}
    </div>
  )
}
