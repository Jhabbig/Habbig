import React, { useEffect, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import StateGridMap from '../components/StateGridMap.jsx'

// Standalone embed views designed to be iframed into newsroom articles and
// partner sites. Each renders a single piece of data — no nav, no shell,
// minimal chrome, attribution at the bottom. The backend serves these
// without an X-Frame-Options DENY so cross-origin embedding works.

function Attribution() {
  return (
    <a
      href="https://midterm.narve.ai"
      target="_blank"
      rel="noopener noreferrer"
      className="block text-[10px] text-stone-400 hover:text-stone-600 mt-2 text-right"
    >
      narve.ai · forecast methodology →
    </a>
  )
}

// /embed/forecast/:raceKey — single race forecast card.
export function EmbedForecast() {
  const { raceKey } = useParams()
  const [params] = useSearchParams()
  const theme = params.get('theme') === 'dark' ? 'dark' : 'light'
  const [forecast, setForecast] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!raceKey) return
    api.forecast(raceKey).then(setForecast).catch(() => setForecast(null)).finally(() => setLoading(false))
  }, [raceKey])

  const bg = theme === 'dark' ? 'bg-stone-900 text-white' : 'bg-white text-stone-900'
  const border = theme === 'dark' ? 'border-stone-700' : 'border-stone-200'

  if (loading) return <div className={`${bg} ${border} border rounded-xl p-4 min-h-[140px] flex items-center justify-center text-stone-400 text-xs`}>Loading…</div>
  if (!forecast || forecast.forecast_d == null) return <div className={`${bg} ${border} border rounded-xl p-4 text-stone-400 text-xs`}>No forecast available for {raceKey}.</div>

  const lean = forecast.forecast_d >= 0.5 ? 'D' : 'R'
  const leanPct = (lean === 'D' ? forecast.forecast_d : 1 - forecast.forecast_d) * 100
  const color = lean === 'D' ? '#2563eb' : '#dc2626'

  return (
    <div className={`${bg} ${border} border rounded-xl p-4`}>
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider opacity-70">
        <span>{forecast.race_type} · {forecast.state}</span>
        <span>narve.ai forecast</span>
      </div>
      <div className="text-sm font-semibold mt-1 truncate">{raceKey.replace(/_/g, ' · ')}</div>
      <div className="flex items-baseline gap-2 mt-2">
        <span className="text-4xl font-bold tabular-nums" style={{ color }}>{lean}</span>
        <span className="text-4xl font-bold tabular-nums">{leanPct.toFixed(1)}%</span>
      </div>
      <div className="mt-3 h-2 rounded-full overflow-hidden flex" style={{ background: theme === 'dark' ? '#44403c' : '#f5f5f4' }}>
        <div style={{ width: `${forecast.forecast_d * 100}%`, background: '#3b82f6' }} />
        <div style={{ width: `${(1 - forecast.forecast_d) * 100}%`, background: '#ef4444' }} />
      </div>
      <div className="flex justify-between text-[10px] opacity-70 mt-1">
        <span>D {(forecast.forecast_d * 100).toFixed(1)}%</span>
        <span>R {((1 - forecast.forecast_d) * 100).toFixed(1)}%</span>
      </div>
      <div className="text-[10px] mt-2 opacity-60">
        {forecast.n_sources} {forecast.n_sources === 1 ? 'source' : 'sources'}
        {forecast.method === 'brier_weighted' ? ' · Brier-weighted' : ' · prior weights'}
      </div>
      <Attribution />
    </div>
  )
}

// /embed/chamber/:chamber — Senate/House/Governor control strip.
export function EmbedChamber() {
  const { chamber } = useParams()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.electionNight().then(setData).catch(() => setData(null)).finally(() => setLoading(false))
  }, [])

  const c = data?.chambers?.[chamber]
  if (loading) return <div className="bg-white border border-stone-200 rounded-xl p-4 min-h-[120px] text-stone-400 text-xs flex items-center justify-center">Loading…</div>
  if (!c || c.total === 0) return <div className="bg-white border border-stone-200 rounded-xl p-4 text-stone-400 text-xs">No {chamber} data.</div>

  const seg = (n) => (n / c.total) * 100
  return (
    <div className="bg-white border border-stone-200 rounded-xl p-4">
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-xs font-semibold text-stone-700 uppercase tracking-wider">{chamber}</div>
        <div className="text-[10px] text-stone-400">{c.total} races</div>
      </div>
      <div className="flex items-baseline gap-4">
        <div>
          <div className="text-3xl font-bold text-blue-600 tabular-nums leading-none">{c.called_d}</div>
          <div className="text-[10px] text-stone-500 uppercase">D called</div>
        </div>
        <div className="text-stone-300 text-2xl">·</div>
        <div>
          <div className="text-3xl font-bold text-rose-600 tabular-nums leading-none">{c.called_r}</div>
          <div className="text-[10px] text-stone-500 uppercase">R called</div>
        </div>
        <div className="ml-auto text-right">
          <div className="text-lg font-bold text-amber-600 tabular-nums leading-none">{c.tossup}</div>
          <div className="text-[10px] text-stone-500 uppercase">tossup</div>
        </div>
      </div>
      <div className="h-3 rounded-full overflow-hidden flex bg-stone-100 mt-3 border border-stone-200">
        <div style={{ width: `${seg(c.called_d)}%`, background: '#3b82f6' }} />
        <div style={{ width: `${seg(c.lean_d)}%`,   background: '#3b82f655' }} />
        <div style={{ width: `${seg(c.tossup)}%`,   background: '#fbbf2455' }} />
        <div style={{ width: `${seg(c.lean_r)}%`,   background: '#ef444455' }} />
        <div style={{ width: `${seg(c.called_r)}%`, background: '#ef4444' }} />
      </div>
      <Attribution />
    </div>
  )
}

// /embed/map/:chamber — full state map (re-uses StateGridMap, no interactivity).
export function EmbedMap() {
  const { chamber } = useParams()
  const [data, setData] = useState(null)
  useEffect(() => {
    api.electionNight().then(setData).catch(() => setData(null))
  }, [])

  const racesByState = {}
  if (data?.races) {
    for (const r of data.races) {
      if ((r.race_type || '').toLowerCase() !== chamber) continue
      const st = (r.state || '').toUpperCase()
      if (st) racesByState[st] = r
    }
  }

  return (
    <div className="bg-white border border-stone-200 rounded-xl p-4">
      <div className="text-xs font-semibold text-stone-700 uppercase tracking-wider mb-2">
        {chamber} · narve.ai forecast map
      </div>
      <StateGridMap racesByState={racesByState} />
      <Attribution />
    </div>
  )
}
