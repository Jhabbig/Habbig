import React, { useState, useEffect, useMemo } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../lib/api'
import { fmtMoney, fmtNum } from '../lib/settings'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend, CartesianGrid, BarChart, Bar, Cell } from 'recharts'
import { ArrowLeft, Clock, Eye, EyeOff, TrendingUp, BarChart3, History, Trophy, MapPin, Users, Building2, Landmark, GraduationCap, Lightbulb, Flag, ShieldCheck, X, Share2, Code2 } from 'lucide-react'
import Comments from '../lib/Comments'
import Movements from '../lib/Movements'

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
  const [districtProfile, setDistrictProfile] = useState(null)
  const [candidates, setCandidates] = useState([])
  const [loading, setLoading] = useState(true)
  const [visibleSources, setVisibleSources] = useState(new Set(Object.keys(sourceColors)))
  const [currentUser, setCurrentUser] = useState(null)
  const [reviewBusy, setReviewBusy] = useState(false)

  useEffect(() => {
    api.me().then(setCurrentUser).catch(() => setCurrentUser(null))
  }, [])

  const isAdmin = currentUser?.tier === 'admin'

  const refetchRace = () => api.race(raceKey).then(setRace).catch(() => {})

  const handleFlagMarket = async (source, sourceId) => {
    if (!race?.race_key || reviewBusy) return
    const note = window.prompt(
      `Mark ${sourceLabels[source] || source} as a WRONG market for this race?\n\nOptional note:`,
      ''
    )
    if (note === null) return  // user cancelled
    setReviewBusy(true)
    try {
      await api.flagMarket(race.race_key, source, sourceId, note || null)
      await refetchRace()
    } catch (e) {
      window.alert(`Failed to flag market: ${e.message}`)
    } finally {
      setReviewBusy(false)
    }
  }

  const handleUnflagMarket = async (source, sourceId) => {
    if (!race?.race_key || reviewBusy) return
    setReviewBusy(true)
    try {
      await api.unflagMarket(race.race_key, source, sourceId)
      await refetchRace()
    } catch (e) {
      window.alert(`Failed to unflag market: ${e.message}`)
    } finally {
      setReviewBusy(false)
    }
  }

  const handleToggleVerify = async () => {
    if (!race?.race_key || reviewBusy) return
    setReviewBusy(true)
    try {
      if (race.verified) {
        await api.unverifyRace(race.race_key)
      } else {
        await api.verifyRace(race.race_key, null)
      }
      await refetchRace()
    } catch (e) {
      window.alert(`Failed to update verification: ${e.message}`)
    } finally {
      setReviewBusy(false)
    }
  }

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
      // Fetch historical election results and district profile for this race_type + state
      if (r?.race_type && r?.state) {
        api.historical({ race_type: r.race_type, state: r.state })
          .then(d => setHistoricalResults(d?.results || []))
          .catch(() => {})
      }
      // Fetch enriched candidates (Wikipedia bios)
      if (r?.race_key) {
        api.raceCandidates(r.race_key)
          .then(c => setCandidates(c?.candidates || []))
          .catch(() => {})
      }
      // Resolve jurisdiction (state, district, or country) and fetch unified profile
      if (r?.state) {
        let jt = null
        let jc = null
        if (r.race_type === 'world') {
          jt = 'country'
          jc = r.state
        } else if (r.race_type === 'house' && r.district) {
          jt = 'us_district'
          jc = `${r.state}-${r.district}`
        } else if (r.state !== 'US') {
          jt = 'us_state'
          jc = r.state
        }
        if (jt && jc) {
          api.jurisdictionProfile(jt, jc)
            .then(p => { if (p && p.found !== false) setDistrictProfile(p) })
            .catch(() => {
              // Fallback to legacy state-only endpoint for backward compat
              if (jt === 'us_state') {
                api.districtProfile(jc).then(p => { if (p && p.found !== false) setDistrictProfile(p) }).catch(() => {})
              }
            })
        }
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
              {race.district && <span className="font-medium text-amber-700">District {parseInt(race.district, 10) || race.district}</span>}
              <span className="capitalize">{race.race_type === 'world' ? 'International' : race.race_type}</span>
              <span className="text-stone-300">{sourceCount} source{sourceCount !== 1 ? 's' : ''}</span>
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {race.verified && (
              <div
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200"
                title={race.verified_by ? `Verified by ${race.verified_by}` : 'Human-verified'}
              >
                <ShieldCheck className="h-3 w-3" />
                Verified
              </div>
            )}
            {maxSpread && (
              <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium ${spreadColor}`}>
                <TrendingUp className="h-3 w-3" />
                {(maxSpread.spread * 100).toFixed(1)}% spread
                <span className="opacity-70">({sourceLabels[maxSpread.sources[0]] || maxSpread.sources[0]} vs {sourceLabels[maxSpread.sources[1]] || maxSpread.sources[1]})</span>
              </div>
            )}
            {isAdmin && (
              <button
                onClick={handleToggleVerify}
                disabled={reviewBusy}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors disabled:opacity-50 ${
                  race.verified
                    ? 'bg-white text-stone-600 border-stone-200 hover:bg-stone-50'
                    : 'bg-emerald-600 text-white border-emerald-600 hover:bg-emerald-700'
                }`}
                title={race.verified ? 'Remove human verification' : 'Mark this source pairing as human-verified'}
              >
                <ShieldCheck className="h-3 w-3" />
                {race.verified ? 'Unverify' : 'Verify match'}
              </button>
            )}
          </div>
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

            const isFlagged = data.flagged === true
            return (
              <div
                key={source}
                className={`rounded-lg p-4 ${isFlagged ? 'bg-red-50/40 border border-red-200/60 opacity-75' : 'bg-stone-50'}`}
              >
                <div className="flex items-center justify-between mb-3">
                  <div className="text-xs font-medium text-stone-600 flex items-center gap-1.5">
                    <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }}></div>
                    <span className={isFlagged ? 'line-through' : ''}>{sourceLabels[source] || source}</span>
                    {isFlagged && (
                      <span
                        className="text-[9px] uppercase font-bold text-red-600 bg-red-100 px-1.5 py-0.5 rounded"
                        title={data.flag_note || 'Marked as wrong by an admin'}
                      >
                        Flagged
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {data.volume > 0 && (
                      <span className="text-[10px] text-stone-400">${(data.volume / 1000).toFixed(0)}k vol</span>
                    )}
                    {tradeable && !isFlagged && (
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
                    {isAdmin && (isFlagged ? (
                      <button
                        onClick={() => handleUnflagMarket(source, data.source_id)}
                        disabled={reviewBusy}
                        title="Restore this market — it does belong to this race after all"
                        className="text-[10px] font-semibold px-2 py-1 rounded-md border border-emerald-200 text-emerald-700 hover:bg-emerald-50 transition-colors disabled:opacity-50 flex items-center gap-1"
                      >
                        <X className="h-3 w-3" /> Unflag
                      </button>
                    ) : (
                      <button
                        onClick={() => handleFlagMarket(source, data.source_id)}
                        disabled={reviewBusy}
                        title={`Flag this ${sourceLabels[source] || source} market as NOT belonging to this race`}
                        className="text-[10px] font-semibold px-2 py-1 rounded-md border border-stone-200 text-stone-500 hover:text-red-600 hover:border-red-300 hover:bg-red-50 transition-colors disabled:opacity-50 flex items-center gap-1"
                      >
                        <Flag className="h-3 w-3" /> Wrong
                      </button>
                    ))}
                  </div>
                </div>
                {isFlagged && data.flag_note && (
                  <p className="text-[10px] text-red-700 mb-2 italic">Note: {data.flag_note}</p>
                )}
                {data.title && data.title !== race.title && (
                  <p className={`text-[10px] mb-2 italic ${isFlagged ? 'text-red-400 line-through' : 'text-stone-400'}`}>{data.title}</p>
                )}
                <div className={`space-y-0.5 ${isFlagged ? 'opacity-60' : ''}`}>
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

      {/* Candidates (Wikipedia bios + market probabilities) */}
      {candidates.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <div className="flex items-center gap-2 mb-4">
            <div className="p-1.5 bg-amber-50 rounded-lg">
              <Users className="h-5 w-5 text-amber-600" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-stone-800">Candidates</h3>
              <p className="text-xs text-stone-400">Market-implied odds + Wikipedia bios. Click any card to read more.</p>
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {candidates.map((c, i) => {
              const pct = ((c.probability || 0) * 100).toFixed(1)
              const isFavorite = i === 0 && (c.probability || 0) > 0.4
              return (
                <a
                  key={c.name}
                  href={c.url || '#'}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={`block rounded-lg p-3 border transition-colors ${
                    isFavorite ? 'bg-amber-50 border-amber-200 hover:bg-amber-100'
                      : 'bg-stone-50 border-stone-100 hover:bg-stone-100'
                  } ${!c.url ? 'pointer-events-none' : ''}`}
                >
                  <div className="flex items-start gap-3">
                    {c.thumbnail ? (
                      <img src={c.thumbnail} alt={c.name} className="w-14 h-14 rounded-full object-cover flex-shrink-0 border border-stone-200" loading="lazy" />
                    ) : (
                      <div className="w-14 h-14 rounded-full bg-stone-200 flex-shrink-0 flex items-center justify-center text-stone-500 text-lg font-semibold">
                        {c.name?.split(' ').map(n => n[0]).slice(0, 2).join('')}
                      </div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-sm font-semibold text-stone-800 truncate">{c.name}</span>
                        <span className={`text-sm font-bold tabular-nums flex-shrink-0 ${
                          isFavorite ? 'text-amber-700' : 'text-stone-700'
                        }`}>{pct}%</span>
                      </div>
                      {c.description && (
                        <div className="text-[10px] text-stone-500 truncate" title={c.description}>{c.description}</div>
                      )}
                      {c.extract && (
                        <p className="text-xs text-stone-600 mt-1 line-clamp-2 leading-snug">{c.extract}</p>
                      )}
                      {c.fec && (
                        <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1.5 text-[10px] text-stone-500">
                          <span title="Total raised">
                            <span className="font-semibold text-emerald-700">
                              {c.fec.receipts >= 1e6 ? `$${(c.fec.receipts / 1e6).toFixed(1)}M` : `$${Math.round(c.fec.receipts / 1e3)}k`}
                            </span> raised
                          </span>
                          <span title="Cash on hand">
                            <span className="font-semibold text-stone-700">
                              {c.fec.cash_on_hand >= 1e6 ? `$${(c.fec.cash_on_hand / 1e6).toFixed(1)}M` : `$${Math.round(c.fec.cash_on_hand / 1e3)}k`}
                            </span> cash
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                </a>
              )
            })}
          </div>
        </div>
      )}

      {/* Jurisdiction Profile (state, district, or country) */}
      {districtProfile && districtProfile.found !== false && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <div className="flex items-center gap-2 mb-4">
            <div className="p-1.5 bg-emerald-50 rounded-lg">
              <MapPin className="h-5 w-5 text-emerald-600" />
            </div>
            <div>
              <h3 className="text-lg font-semibold text-stone-800">
                {districtProfile.name || districtProfile.state}
                {' — '}
                {districtProfile.jurisdiction_type === 'country' ? 'Country Profile'
                  : districtProfile.jurisdiction_type === 'us_district' ? 'District Profile'
                  : 'State Profile'}
              </h3>
              <p className="text-xs text-stone-400">Demographics, economy, infrastructure, political history</p>
            </div>
          </div>

          {/* Population & Demographics */}
          {districtProfile.demographics && (
            <div className="mb-5">
              <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                <Users className="h-3.5 w-3.5" /> Demographics
                {districtProfile.population?.total > 0 && (
                  <span className="text-stone-400 font-normal ml-1">
                    — Pop. {(districtProfile.population.total / 1_000_000).toFixed(1)}M
                    {districtProfile.population.rank > 0 && ` (rank #${districtProfile.population.rank})`}
                  </span>
                )}
              </h4>
              <p className="text-sm text-stone-600 leading-relaxed mb-3">{districtProfile.demographics.summary}</p>
              {districtProfile.demographics.white && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {[
                    { label: 'White', value: districtProfile.demographics.white },
                    { label: 'Black', value: districtProfile.demographics.black },
                    { label: 'Hispanic', value: districtProfile.demographics.hispanic },
                    { label: 'Asian', value: districtProfile.demographics.asian },
                  ].filter(d => d.value).map(d => (
                    <div key={d.label} className="bg-stone-50 rounded-lg px-3 py-2 text-center">
                      <div className="text-xs text-stone-400">{d.label}</div>
                      <div className="text-sm font-semibold text-stone-800">{d.value}%</div>
                    </div>
                  ))}
                  {districtProfile.demographics.median_age && (
                    <div className="bg-stone-50 rounded-lg px-3 py-2 text-center">
                      <div className="text-xs text-stone-400">Median Age</div>
                      <div className="text-sm font-semibold text-stone-800">{districtProfile.demographics.median_age}</div>
                    </div>
                  )}
                  {districtProfile.demographics.urban_pct && (
                    <div className="bg-stone-50 rounded-lg px-3 py-2 text-center">
                      <div className="text-xs text-stone-400">Urban</div>
                      <div className="text-sm font-semibold text-stone-800">{districtProfile.demographics.urban_pct}%</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
            {/* Economy */}
            {districtProfile.economy && (
              <div className="bg-stone-50 rounded-lg p-4">
                <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                  <Building2 className="h-3.5 w-3.5" /> Economy
                  {districtProfile.economy.gdp_billions && (
                    <span className="text-stone-400 font-normal ml-1">— GDP ${districtProfile.economy.gdp_billions}B</span>
                  )}
                </h4>
                <p className="text-xs text-stone-600 leading-relaxed mb-2">{districtProfile.economy.summary}</p>
                {districtProfile.economy.top_industries?.length > 0 && (
                  <div className="mt-2">
                    <div className="text-[10px] text-stone-400 uppercase mb-1">Top Industries</div>
                    <div className="flex flex-wrap gap-1">
                      {districtProfile.economy.top_industries.map((ind, i) => (
                        <span key={i} className="text-[10px] bg-white border border-stone-200 text-stone-600 px-2 py-0.5 rounded">{ind}</span>
                      ))}
                    </div>
                  </div>
                )}
                {(districtProfile.economy.median_household_income || districtProfile.economy.gdp_per_capita || districtProfile.economy.unemployment_rate) && (
                  <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-xs text-stone-500">
                    {districtProfile.economy.median_household_income && (
                      <span>Median Income: <strong className="text-stone-700">{fmtMoney(districtProfile.economy.median_household_income)}</strong></span>
                    )}
                    {districtProfile.economy.gdp_per_capita && (
                      <span>GDP / Capita: <strong className="text-stone-700">{fmtMoney(districtProfile.economy.gdp_per_capita)}</strong></span>
                    )}
                    {districtProfile.economy.unemployment_rate != null && (
                      <span>Unemployment: <strong className="text-stone-700">{districtProfile.economy.unemployment_rate}%</strong></span>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* Infrastructure */}
            {districtProfile.infrastructure && (
              <div className="bg-stone-50 rounded-lg p-4">
                <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                  <Landmark className="h-3.5 w-3.5" /> Infrastructure
                </h4>
                <p className="text-xs text-stone-600 leading-relaxed mb-2">{districtProfile.infrastructure.summary}</p>
                {districtProfile.infrastructure.major_airports?.length > 0 && (
                  <div className="mt-2">
                    <div className="text-[10px] text-stone-400 uppercase mb-1">Airports</div>
                    <div className="text-xs text-stone-600">{districtProfile.infrastructure.major_airports.join(', ')}</div>
                  </div>
                )}
                {districtProfile.infrastructure.military_bases?.length > 0 && (
                  <div className="mt-2">
                    <div className="text-[10px] text-stone-400 uppercase mb-1">Military Bases</div>
                    <div className="text-xs text-stone-600">{districtProfile.infrastructure.military_bases.join(', ')}</div>
                  </div>
                )}
                {districtProfile.infrastructure.ports?.length > 0 && (
                  <div className="mt-2">
                    <div className="text-[10px] text-stone-400 uppercase mb-1">Ports</div>
                    <div className="text-xs text-stone-600">{districtProfile.infrastructure.ports.join(', ')}</div>
                  </div>
                )}
              </div>
            )}

            {/* Political History */}
            {districtProfile.political_history && (
              <div className="bg-stone-50 rounded-lg p-4">
                <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                  <Landmark className="h-3.5 w-3.5" /> Political History
                  {districtProfile.political_history.cook_pvi && (
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ml-1 ${
                      districtProfile.political_history.cook_pvi.startsWith('D') ? 'bg-blue-100 text-blue-700' :
                      districtProfile.political_history.cook_pvi.startsWith('R') ? 'bg-red-100 text-red-700' :
                      'bg-amber-100 text-amber-700'
                    }`}>PVI: {districtProfile.political_history.cook_pvi}</span>
                  )}
                </h4>
                <p className="text-xs text-stone-600 leading-relaxed mb-2">{districtProfile.political_history.summary}</p>
                <div className="space-y-1.5 mt-2">
                  {districtProfile.political_history['2024_presidential'] && (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-stone-400">2024 Presidential</span>
                      <span className="font-medium text-stone-700">{districtProfile.political_history['2024_presidential'].winner} ({districtProfile.political_history['2024_presidential'].margin})</span>
                    </div>
                  )}
                  {districtProfile.political_history['2020_presidential'] && (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-stone-400">2020 Presidential</span>
                      <span className="font-medium text-stone-700">{districtProfile.political_history['2020_presidential'].winner} ({districtProfile.political_history['2020_presidential'].margin})</span>
                    </div>
                  )}
                  {districtProfile.political_history.governor_since && (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-stone-400">Governor</span>
                      <span className="font-medium text-stone-700">{districtProfile.political_history.governor_since}</span>
                    </div>
                  )}
                  {districtProfile.political_history.state_legislature && (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-stone-400">Legislature</span>
                      <span className="font-medium text-stone-700">{districtProfile.political_history.state_legislature}</span>
                    </div>
                  )}
                  {districtProfile.political_history.electoral_votes && (
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-stone-400">Electoral Votes</span>
                      <span className="font-bold text-stone-800">{districtProfile.political_history.electoral_votes}</span>
                    </div>
                  )}
                </div>
                {districtProfile.political_history.trend && (
                  <div className="mt-2 pt-2 border-t border-stone-200">
                    <div className="text-[10px] text-stone-400 uppercase mb-1">Trend</div>
                    <p className="text-xs text-stone-600">{districtProfile.political_history.trend}</p>
                  </div>
                )}
                {districtProfile.political_history.wikipedia_url && (
                  <div className="mt-2 pt-2 border-t border-stone-200">
                    <a
                      href={districtProfile.political_history.wikipedia_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[10px] text-emerald-700 hover:text-emerald-900 font-medium"
                    >
                      Read more on Wikipedia &rarr;
                    </a>
                  </div>
                )}
              </div>
            )}

            {/* Education + Geography */}
            <div className="space-y-4">
              {districtProfile.education && (
                <div className="bg-stone-50 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                    <GraduationCap className="h-3.5 w-3.5" /> Education
                    {districtProfile.education.bachelors_or_higher_pct && (
                      <span className="text-stone-400 font-normal ml-1">— {districtProfile.education.bachelors_or_higher_pct}% bachelor's+</span>
                    )}
                  </h4>
                  <p className="text-xs text-stone-600 leading-relaxed mb-2">{districtProfile.education.summary}</p>
                  {districtProfile.education.major_universities?.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {districtProfile.education.major_universities.map((u, i) => (
                        <span key={i} className="text-[10px] bg-white border border-stone-200 text-stone-600 px-2 py-0.5 rounded">{u}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {districtProfile.geography && (
                <div className="bg-stone-50 rounded-lg p-4">
                  <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                    <MapPin className="h-3.5 w-3.5" /> Geography
                  </h4>
                  {districtProfile.geography.region && (
                    <div className="text-xs text-stone-500 mb-1">Region: <strong className="text-stone-700">{districtProfile.geography.region}</strong></div>
                  )}
                  {districtProfile.geography.terrain && (
                    <p className="text-xs text-stone-600 mb-1">{districtProfile.geography.terrain}</p>
                  )}
                  {districtProfile.geography.climate && (
                    <p className="text-xs text-stone-500">{districtProfile.geography.climate}</p>
                  )}
                  {districtProfile.geography.major_cities?.length > 0 && (
                    <div className="mt-2">
                      <div className="text-[10px] text-stone-400 uppercase mb-1">Major Cities</div>
                      <div className="text-xs text-stone-600">{districtProfile.geography.major_cities.join(', ')}</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Key Facts */}
          {districtProfile.key_facts?.length > 0 && (
            <div className="mt-4 pt-4 border-t border-stone-100">
              <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-2 flex items-center gap-1.5">
                <Lightbulb className="h-3.5 w-3.5" /> Key Facts
              </h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                {districtProfile.key_facts.map((fact, i) => (
                  <div key={i} className="flex items-start gap-2 text-xs text-stone-600">
                    <span className="text-emerald-500 mt-0.5 flex-shrink-0">&#x2022;</span>
                    <span>{fact}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Past Winners — per-race results parsed from Wikipedia Election boxes */}
          {(() => {
            const pastWinners = race?.race_type === 'senate' ? districtProfile.senate_past_winners
              : race?.race_type === 'governor' ? districtProfile.governor_past_winners
              : districtProfile.past_winners
            if (!pastWinners?.length) return null
            const label = race?.race_type === 'senate' ? 'Senate' : race?.race_type === 'governor' ? 'Governor' : 'District'
            return (
            <div className="mt-5 pt-4 border-t border-stone-100">
              <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Trophy className="h-3.5 w-3.5" /> Past {label} Winners
              </h4>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {pastWinners.map((w, i) => {
                  const isDem = w.party?.toLowerCase().startsWith('democrat')
                  const isRep = w.party?.toLowerCase().startsWith('republic')
                  const cardColor = isDem ? 'bg-blue-50 border-blue-200' : isRep ? 'bg-red-50 border-red-200' : 'bg-stone-50 border-stone-200'
                  const partyBadge = isDem ? 'bg-blue-600 text-white' : isRep ? 'bg-red-600 text-white' : 'bg-stone-600 text-white'
                  return (
                    <a
                      key={i}
                      href={w.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={`block border rounded-lg p-3 hover:shadow-sm transition-shadow ${cardColor}`}
                    >
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-xs font-bold text-stone-500">{w.year}</span>
                        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${partyBadge}`}>
                          {isDem ? 'D' : isRep ? 'R' : (w.party?.[0] || '?')}
                        </span>
                      </div>
                      <div className="text-sm font-semibold text-stone-800 leading-tight">{w.candidate}</div>
                      <div className="text-xs text-stone-500 mt-0.5">
                        {w.percentage != null && <>{w.percentage.toFixed(1)}%</>}
                        {w.votes != null && <> &middot; {(w.votes / 1000).toFixed(0)}k votes</>}
                      </div>
                      <div className="flex flex-wrap gap-1 mt-1.5">
                        {w.incumbent && (
                          <span className="text-[9px] font-semibold text-stone-600 bg-white border border-stone-200 px-1.5 py-0.5 rounded">incumbent</span>
                        )}
                        {w.flip_from && (
                          <span className="text-[9px] font-semibold text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded">
                            flipped from {w.flip_from[0]}
                          </span>
                        )}
                      </div>
                    </a>
                  )
                })}
              </div>
              {/* Trend analysis */}
              {pastWinners.length >= 2 && (() => {
                const dWins = pastWinners.filter(w => w.party?.toLowerCase().startsWith('democrat')).length
                const rWins = pastWinners.filter(w => w.party?.toLowerCase().startsWith('republic')).length
                const flips = pastWinners.filter(w => w.flip_from).length
                const latestPct = pastWinners[0]?.percentage
                const prevPct = pastWinners[1]?.percentage
                const sameParty = pastWinners[0]?.party === pastWinners[1]?.party
                const marginShift = (latestPct != null && prevPct != null && sameParty) ? (latestPct - prevPct).toFixed(1) : null
                return (
                  <div className="mt-3 pt-2 border-t border-stone-100 flex flex-wrap items-center gap-3 text-xs text-stone-500">
                    <span>{pastWinners.length} elections:</span>
                    {dWins > 0 && <span className="text-blue-700 font-semibold">{dWins}D</span>}
                    {rWins > 0 && <span className="text-red-700 font-semibold">{rWins}R</span>}
                    {flips > 0 && <span className="text-amber-600 font-semibold">{flips} flip{flips > 1 ? 's' : ''}</span>}
                    {marginShift !== null && (
                      <span className={Number(marginShift) > 0 ? 'text-emerald-600' : Number(marginShift) < 0 ? 'text-red-600' : ''}>
                        {Number(marginShift) > 0 ? '+' : ''}{marginShift}% margin shift
                      </span>
                    )}
                  </div>
                )
              })()}
            </div>
            )
          })()}

          {/* Recent Elections — Wikipedia history (US states, US districts, countries) */}
          {districtProfile.recent_elections?.length > 0 && (
            <div className="mt-5 pt-4 border-t border-stone-100">
              <h4 className="text-xs font-semibold text-stone-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <History className="h-3.5 w-3.5" /> Recent Elections
              </h4>
              <div className="space-y-3">
                {districtProfile.recent_elections.slice(0, 6).map((el, i) => (
                  <a
                    key={i}
                    href={el.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block bg-stone-50 hover:bg-stone-100 transition-colors rounded-lg p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          {el.year && <span className="text-[10px] font-bold text-stone-500 bg-white border border-stone-200 px-1.5 py-0.5 rounded">{el.year}</span>}
                          <span className="text-sm font-semibold text-stone-800 truncate">{el.title}</span>
                        </div>
                        {el.extract && <p className="text-xs text-stone-600 leading-relaxed line-clamp-3">{el.extract}</p>}
                      </div>
                      {el.thumbnail && (
                        <img src={el.thumbnail} alt="" className="w-16 h-16 object-cover rounded flex-shrink-0" loading="lazy" />
                      )}
                    </div>
                  </a>
                ))}
              </div>
            </div>
          )}

          {/* Data sources footer */}
          <div className="mt-3 text-[10px] text-stone-300 text-right flex items-center justify-end gap-2">
            {districtProfile._data_sources?.length > 0 && (
              <span>Sources: {districtProfile._data_sources.join(' \u00B7 ')}</span>
            )}
            {districtProfile.updated_at && (
              <span>Updated: {districtProfile.updated_at?.slice(0, 10)}</span>
            )}
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
        <section aria-labelledby="price-history-heading"
          className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 sm:p-6 mb-6">
          <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
            <h3 id="price-history-heading" className="text-lg font-semibold text-stone-800 flex items-center gap-2">
              <Clock className="h-5 w-5 text-stone-500" aria-hidden="true" />Price History
            </h3>
            <div className="flex gap-1.5 flex-wrap" role="group" aria-label="Toggle data sources">
              {availableSources.map(source => (
                <button key={source} onClick={() => toggleSource(source)}
                  aria-pressed={visibleSources.has(source)}
                  aria-label={`${visibleSources.has(source) ? 'Hide' : 'Show'} ${sourceLabels[source] || source}`}
                  className={`flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-all border ${
                    visibleSources.has(source) ? 'border-stone-200 bg-white text-stone-700 shadow-sm' : 'border-transparent bg-stone-100 text-stone-400'
                  }`}>
                  {visibleSources.has(source) ? <Eye className="h-3 w-3" aria-hidden="true" /> : <EyeOff className="h-3 w-3" aria-hidden="true" />}
                  <div className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: sourceColors[source] }} aria-hidden="true"></div>
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
        </section>
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
                    <td className="py-2 px-3 text-right text-xs text-stone-400">{poll.sample_size != null ? fmtNum(poll.sample_size) : '—'}</td>
                    <td className="py-2 px-3 text-right text-xs text-stone-400">{poll.end_date || poll.start_date || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {polls.length > 30 && <p className="text-xs text-stone-400 text-center py-2">Showing 30 of {polls.length} polls</p>}
          </div>
        </div>
      )}

      {race?.race_key && <Movements raceKey={race.race_key} hours={24} />}
      {race?.race_key && <Comments raceKey={race.race_key} currentUser={currentUser} />}

      {race?.race_key && (
        <section aria-labelledby="share-heading"
          className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 sm:p-6 mb-6">
          <h3 id="share-heading" className="text-sm font-semibold text-stone-800 flex items-center gap-2 mb-3">
            <Share2 className="h-4 w-4 text-stone-500" aria-hidden="true" />Share &amp; embed
          </h3>
          <div className="space-y-2 text-xs">
            <button onClick={() => {
                const url = `${window.location.origin}/race/${race.race_key}`
                navigator.clipboard?.writeText(url)
              }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-stone-100 text-stone-700 hover:bg-stone-200">
              <Share2 className="h-3.5 w-3.5" aria-hidden="true" /> Copy link
            </button>
            <details className="mt-2">
              <summary className="cursor-pointer text-stone-600 inline-flex items-center gap-1.5">
                <Code2 className="h-3.5 w-3.5" aria-hidden="true" /> Embed code
              </summary>
              <pre className="mt-2 p-2 bg-stone-50 border border-stone-100 rounded-md overflow-x-auto text-[11px] text-stone-700">{`<iframe src="${window.location.origin}/embed/race/${race.race_key}" width="480" height="280" frameborder="0" style="border:0;border-radius:12px"></iframe>`}</pre>
            </details>
          </div>
        </section>
      )}
    </div>
  )
}
