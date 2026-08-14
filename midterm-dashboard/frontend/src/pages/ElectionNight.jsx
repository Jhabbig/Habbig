import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useDataStream } from '../lib/useDataStream.js'
import { Sparkles, AlertTriangle, Wallet, Zap, Radio } from 'lucide-react'

const CALL_STYLES = {
  called_d: { label: 'Called D',  bg: '#1d4ed8', fg: '#ffffff', dot: '#3b82f6' },
  called_r: { label: 'Called R',  bg: '#b91c1c', fg: '#ffffff', dot: '#ef4444' },
  lean_d:   { label: 'Lean D',    bg: '#1e293b', fg: '#93c5fd', dot: '#3b82f6' },
  lean_r:   { label: 'Lean R',    bg: '#1e293b', fg: '#fca5a5', dot: '#ef4444' },
  tossup:   { label: 'Tossup',    bg: '#27272a', fg: '#fbbf24', dot: '#fbbf24' },
}

function CallChip({ state }) {
  const s = CALL_STYLES[state] || CALL_STYLES.tossup
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider"
      style={{ background: s.bg, color: s.fg }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ background: s.dot }} />
      {s.label}
    </span>
  )
}

// Big horizontal control-strip bar: called_d ▒ lean_d ▒ tossup ▒ lean_r ▒ called_r
function ChamberStrip({ chamber, title }) {
  if (!chamber || !chamber.total) return null
  const total = chamber.total
  const seg = (n) => (n / total) * 100
  return (
    <div className="bg-stone-900/60 border border-stone-700 rounded-xl p-4">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="text-sm font-semibold text-stone-200 uppercase tracking-wider">{title}</h3>
        <span className="text-[10px] text-stone-500">{total} races</span>
      </div>
      <div className="flex items-baseline gap-4 mb-2">
        <div>
          <div className="text-3xl font-bold text-blue-400 tabular-nums leading-none">{chamber.called_d}</div>
          <div className="text-[10px] text-stone-500 uppercase">D called</div>
        </div>
        <div className="text-stone-600 text-2xl">·</div>
        <div>
          <div className="text-3xl font-bold text-rose-400 tabular-nums leading-none">{chamber.called_r}</div>
          <div className="text-[10px] text-stone-500 uppercase">R called</div>
        </div>
        <div className="ml-auto text-right">
          <div className="text-lg font-bold text-amber-300 tabular-nums leading-none">{chamber.tossup}</div>
          <div className="text-[10px] text-stone-500 uppercase">tossup</div>
        </div>
      </div>
      <div className="h-3 rounded-full overflow-hidden flex bg-stone-800 border border-stone-700">
        <div style={{ width: `${seg(chamber.called_d)}%`, background: '#3b82f6' }} title={`Called D: ${chamber.called_d}`} />
        <div style={{ width: `${seg(chamber.lean_d)}%`,   background: '#3b82f655' }} title={`Lean D: ${chamber.lean_d}`} />
        <div style={{ width: `${seg(chamber.tossup)}%`,   background: '#fbbf2455' }} title={`Tossup: ${chamber.tossup}`} />
        <div style={{ width: `${seg(chamber.lean_r)}%`,   background: '#ef444455' }} title={`Lean R: ${chamber.lean_r}`} />
        <div style={{ width: `${seg(chamber.called_r)}%`, background: '#ef4444' }} title={`Called R: ${chamber.called_r}`} />
      </div>
      <div className="flex justify-between text-[10px] text-stone-500 mt-1.5 tabular-nums">
        <span>D floor {chamber.d_floor} · ceiling {chamber.d_ceiling}</span>
        <span>R floor {chamber.r_floor} · ceiling {chamber.r_ceiling}</span>
      </div>
    </div>
  )
}

function fmtUsdShort(usd) {
  const n = Number(usd) || 0
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`
  return `$${Math.round(n)}`
}

function RaceRow({ race }) {
  const sm = race.smart_money || {}
  const lean = race.forecast_d >= 0.5 ? 'D' : 'R'
  const smDiverges = sm.available && sm.direction && sm.direction !== lean
  const gap = race.polling_gap_pp
  const gapColor = gap == null ? 'text-stone-500' : Math.abs(gap) >= 3 ? 'text-amber-300' : 'text-stone-400'

  return (
    <Link
      to={`/race/${race.race_key}`}
      className={`block px-3 py-2 rounded-lg transition-colors text-sm border ${
        smDiverges
          ? 'border-amber-500/50 bg-amber-500/5 hover:bg-amber-500/10'
          : 'border-stone-800 bg-stone-900/50 hover:bg-stone-800'
      }`}
    >
      <div className="flex items-center gap-3">
        <div className="w-24 flex-shrink-0">
          <CallChip state={race.call_state} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-medium text-stone-100 truncate">{race.race_key?.replace(/_/g, ' · ')}</div>
          <div className="text-[10px] text-stone-500">
            {race.n_sources} src
            {race.method === 'brier_weighted' ? ' · Brier' : ''}
            {sm.available && (
              <>
                {' · '}
                <Wallet className="inline h-3 w-3 -mt-0.5" />
                {' '}{sm.direction} {fmtUsdShort(sm.total_smart_usd)}
              </>
            )}
          </div>
        </div>
        <div className="w-24 text-right tabular-nums">
          <div className="text-lg font-bold" style={{ color: lean === 'D' ? '#60a5fa' : '#fca5a5' }}>
            {lean} {(lean === 'D' ? race.forecast_d : 1 - race.forecast_d) * 100 | 0}%
          </div>
          {gap != null && (
            <div className={`text-[10px] ${gapColor}`}>
              vs poll {gap > 0 ? '+' : ''}{gap.toFixed(1)}pp
            </div>
          )}
        </div>
        {smDiverges && <AlertTriangle className="h-4 w-4 text-amber-400 flex-shrink-0" />}
      </div>
    </Link>
  )
}

export default function ElectionNight() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [error, setError] = useState(null)

  function refresh() {
    api.electionNight()
      .then((d) => { setData(d); setLastUpdated(new Date()) })
      .catch((e) => setError(e.message || String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    refresh()
    // Belt-and-braces poll every 30s in case SSE drops.
    const id = setInterval(refresh, 30_000)
    return () => clearInterval(id)
  }, [])

  // SSE pushes from data refresh + divergence loops trigger an immediate
  // re-fetch; sub-minute latency without polling pressure.
  useDataStream(() => refresh())

  if (loading) {
    return <div className="text-stone-400 text-center py-12">Loading election-night view…</div>
  }
  if (error || !data) {
    return <div className="text-rose-400 text-center py-12">Couldn’t load: {error || 'no data'}</div>
  }

  const races = data.races || []
  const tossups = races.filter((r) => r.call_state === 'tossup')
  const recentlyMoved = races
    .filter((r) => r.smart_money?.available && r.smart_money.direction
      && ((r.forecast_d >= 0.5 ? 'D' : 'R') !== r.smart_money.direction))

  // Render the page in a dark/race-night theme so it visually pops away
  // from the day-mode dashboard.
  return (
    <div className="text-stone-100">
      <div className="flex flex-wrap items-baseline justify-between gap-3 mb-5">
        <div>
          <h1 className="text-3xl font-semibold flex items-center gap-2">
            <Sparkles className="h-6 w-6 text-amber-300" />
            Election night
          </h1>
          <p className="text-sm text-stone-400 mt-1">
            narve.ai synthetic calls · ensemble forecast + smart-money agreement + ensemble confidence.
            <span className="text-stone-500"> Not a decision-desk call.</span>
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-stone-400">
          <Radio className="h-3 w-3 text-emerald-400 animate-pulse" />
          live · {lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
        </div>
      </div>

      <div className="grid md:grid-cols-3 gap-4 mb-6">
        <ChamberStrip chamber={data.chambers?.senate}   title="Senate" />
        <ChamberStrip chamber={data.chambers?.house}    title="House" />
        <ChamberStrip chamber={data.chambers?.governor} title="Governor" />
      </div>

      <div className="bg-stone-900/60 border border-stone-700 rounded-xl p-4 mb-6 grid grid-cols-3 sm:grid-cols-4 gap-4">
        <div>
          <div className="text-[10px] text-stone-500 uppercase tracking-wider">Total</div>
          <div className="text-2xl font-bold tabular-nums">{data.counts?.total_races ?? 0}</div>
        </div>
        <div>
          <div className="text-[10px] text-stone-500 uppercase tracking-wider">Called</div>
          <div className="text-2xl font-bold text-emerald-300 tabular-nums">{data.counts?.called ?? 0}</div>
        </div>
        <div>
          <div className="text-[10px] text-stone-500 uppercase tracking-wider">Leans</div>
          <div className="text-2xl font-bold text-stone-200 tabular-nums">{data.counts?.leans ?? 0}</div>
        </div>
        <div>
          <div className="text-[10px] text-stone-500 uppercase tracking-wider">Tossups</div>
          <div className="text-2xl font-bold text-amber-300 tabular-nums">{data.counts?.tossups ?? 0}</div>
        </div>
      </div>

      {recentlyMoved.length > 0 && (
        <div className="bg-amber-500/5 border border-amber-500/30 rounded-xl p-4 mb-6">
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle className="h-4 w-4 text-amber-400" />
            <h3 className="text-sm font-semibold text-amber-200 uppercase tracking-wider">
              Smart-money divergence ({recentlyMoved.length})
            </h3>
          </div>
          <div className="grid sm:grid-cols-2 gap-2">
            {recentlyMoved.slice(0, 8).map((r) => <RaceRow key={r.race_key} race={r} />)}
          </div>
        </div>
      )}

      {tossups.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold text-stone-300 uppercase tracking-wider mb-3 flex items-center gap-2">
            <Zap className="h-4 w-4 text-amber-300" />
            Tossups ({tossups.length})
          </h3>
          <div className="grid sm:grid-cols-2 gap-2">
            {tossups.slice(0, 12).map((r) => <RaceRow key={r.race_key} race={r} />)}
          </div>
        </div>
      )}

      <div>
        <h3 className="text-sm font-semibold text-stone-300 uppercase tracking-wider mb-3">
          All races
        </h3>
        <div className="grid sm:grid-cols-2 gap-2">
          {races.map((r) => <RaceRow key={r.race_key} race={r} />)}
        </div>
      </div>
    </div>
  )
}
