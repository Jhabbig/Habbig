import React, { useState, useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { fmtCount } from '../lib/settings'
import { History, Trophy, ArrowRight } from 'lucide-react'

const PARTY_COLORS = {
  D: { bg: 'bg-blue-100', text: 'text-blue-700', bar: '#3b82f6' },
  R: { bg: 'bg-red-100', text: 'text-red-700', bar: '#ef4444' },
  I: { bg: 'bg-purple-100', text: 'text-purple-700', bar: '#a855f7' },
}

const fmtVotes = (v) => (v ? fmtCount(v) : '\u2014')

function ResultCard({ r }) {
  const winner = PARTY_COLORS[r.party] || PARTY_COLORS.I
  const loser = PARTY_COLORS[r.runner_up_party] || PARTY_COLORS.I
  const total = (r.winner_votes || 0) + (r.runner_up_votes || 0)
  const winnerPct = total ? (r.winner_votes / total) * 100 : r.winner_pct
  const raceKey = `${r.race_type}_${r.state}`

  return (
    <Link to={`/race/${raceKey}`} className="block group">
      <div className="bg-white border border-stone-100 rounded-xl shadow-sm p-5 transition-all group-hover:shadow-md group-hover:border-stone-200 group-hover:scale-[1.01]">
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-xs text-stone-400 uppercase tracking-wide">{r.year}</div>
            <div className="font-semibold text-stone-800 capitalize">{r.race_type} — {r.state}</div>
          </div>
          <div className="flex items-center gap-2">
            <Trophy className="h-5 w-5 text-amber-500" />
            <ArrowRight className="h-4 w-4 text-stone-300 group-hover:text-stone-500 transition-colors" />
          </div>
        </div>

        <div className="space-y-2 mb-3">
          <div>
            <div className="flex items-center justify-between text-sm mb-1">
              <div className="flex items-center gap-2">
                <span className={`${winner.bg} ${winner.text} px-1.5 py-0.5 rounded text-xs font-bold`}>{r.party}</span>
                <span className="font-medium text-stone-800">{r.winner}</span>
              </div>
              <span className="font-semibold text-stone-900">{r.winner_pct}%</span>
            </div>
            <div className="w-full bg-stone-100 rounded-full h-2">
              <div className="h-2 rounded-full" style={{ width: `${winnerPct}%`, backgroundColor: winner.bar }} />
            </div>
            <div className="text-xs text-stone-400 mt-0.5">{fmtVotes(r.winner_votes)} votes</div>
          </div>

          <div>
            <div className="flex items-center justify-between text-sm mb-1">
              <div className="flex items-center gap-2">
                <span className={`${loser.bg} ${loser.text} px-1.5 py-0.5 rounded text-xs font-bold`}>{r.runner_up_party}</span>
                <span className="text-stone-600">{r.runner_up}</span>
              </div>
              <span className="text-stone-600">{r.runner_up_pct}%</span>
            </div>
            <div className="w-full bg-stone-100 rounded-full h-2">
              <div className="h-2 rounded-full opacity-60" style={{ width: `${100 - winnerPct}%`, backgroundColor: loser.bar }} />
            </div>
            <div className="text-xs text-stone-400 mt-0.5">{fmtVotes(r.runner_up_votes)} votes</div>
          </div>
        </div>

        <div className="flex items-center justify-between text-xs text-stone-500 border-t border-stone-100 pt-2">
          <span>Margin: <span className="font-semibold text-stone-800">{r.margin_pct}%</span></span>
          <span className="text-stone-400 group-hover:text-stone-600 transition-colors text-[10px]">View district profile &rarr;</span>
        </div>
      </div>
    </Link>
  )
}

export default function Historical() {
  const [data, setData] = useState({ results: [], filters: { years: [], race_types: [], states: [] } })
  const [loading, setLoading] = useState(true)
  const [year, setYear] = useState('')
  const [raceType, setRaceType] = useState('')
  const [stateFilter, setStateFilter] = useState('')

  useEffect(() => {
    api.historical().then(setData).catch(() => {}).finally(() => setLoading(false))
  }, [])

  const filtered = useMemo(() => (data.results || []).filter(r =>
    (!year || r.year === Number(year)) &&
    (!raceType || r.race_type === raceType) &&
    (!stateFilter || r.state === stateFilter)
  ), [data.results, year, raceType, stateFilter])

  return (
    <div>
      <div className="flex items-center gap-3 mb-6 flex-wrap">
        <div className="p-2 bg-amber-50 rounded-lg">
          <History className="h-6 w-6 text-amber-600" aria-hidden="true" />
        </div>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl font-bold text-stone-900 tracking-tight">Historical Results</h1>
          <p className="text-stone-500 text-sm">Past election winners, vote totals, and margins</p>
        </div>
        {data.last_verified && (
          <span className="text-[11px] text-stone-400 bg-stone-50 border border-stone-200 px-2 py-1 rounded-md"
            title="Date this hand-curated dataset was last reviewed">
            Verified {data.last_verified}
          </span>
        )}
      </div>

      <div className="bg-white border border-stone-100 rounded-xl shadow-sm p-4 mb-4 grid grid-cols-1 md:grid-cols-3 gap-3">
        <div>
          <label className="text-xs text-stone-400 block mb-1">Year</label>
          <select value={year} onChange={e => setYear(e.target.value)}
            className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
            <option value="">All years</option>
            {data.filters.years.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-stone-400 block mb-1">Race type</label>
          <select value={raceType} onChange={e => setRaceType(e.target.value)}
            className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
            <option value="">All races</option>
            {data.filters.race_types.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div>
          <label className="text-xs text-stone-400 block mb-1">State</label>
          <select value={stateFilter} onChange={e => setStateFilter(e.target.value)}
            className="w-full bg-stone-50 border border-stone-200 rounded-lg px-3 py-1.5 text-sm text-stone-700">
            <option value="">All states</option>
            {data.filters.states.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      </div>

      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1,2,3,4,5,6].map(i => <div key={i} className="bg-white border border-stone-100 rounded-xl h-48 animate-pulse" />)}
        </div>
      ) : filtered.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((r, i) => <ResultCard key={i} r={r} />)}
        </div>
      ) : (
        <div className="bg-white border border-stone-100 rounded-xl p-12 text-center text-stone-400">
          No results match your filters.
        </div>
      )}
    </div>
  )
}
