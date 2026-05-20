import React, { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { useDataStream } from '../lib/useDataStream.js'
import StateGridMap from '../components/StateGridMap.jsx'
import { Sparkles, ArrowRight, RotateCcw, AlertTriangle, Wallet } from 'lucide-react'

const CHAMBER_OPTIONS = [
  { key: 'senate',   label: 'Senate' },
  { key: 'governor', label: 'Governor' },
  { key: 'house',    label: 'House' },
]

function fmtUsd(usd) {
  const n = Number(usd) || 0
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`
  return `$${Math.round(n)}`
}

export default function MapPage() {
  const [chamber, setChamber] = useState('senate')
  const [data, setData] = useState(null)        // /data/election-night response
  const [jointSummary, setJointSummary] = useState(null)
  const [conditional, setConditional] = useState(null)  // active conditional view
  const [conditionedKey, setConditionedKey] = useState(null)
  const [conditionedOutcome, setConditionedOutcome] = useState(null)
  const [hovered, setHovered] = useState(null)
  const [selected, setSelected] = useState(null)
  const [loading, setLoading] = useState(true)
  // Wave-scenario state. Live-updates the map as the slider moves.
  const [waveSwing, setWaveSwing] = useState(0)
  const [waveData, setWaveData] = useState(null)

  function refresh() {
    Promise.all([
      api.electionNight(),
      api.forecastJointSummary().catch(() => null),
    ]).then(([en, js]) => {
      setData(en)
      setJointSummary(js)
    }).finally(() => setLoading(false))
  }

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 60_000)
    return () => clearInterval(id)
  }, [])
  useDataStream(() => refresh())

  // Filter to the active chamber and index by state. Source precedence:
  //   1. Conditional view (if active) — highest priority
  //   2. Wave scenario (if non-zero) — applied to base forecasts
  //   3. Base election-night snapshot
  const racesByState = useMemo(() => {
    if (!data) return {}
    let rows = data.races || []
    if (conditional?.races) {
      rows = conditional.races
    } else if (waveData?.races) {
      // Wave response carries the swung forecasts but not call_state. Recompute
      // a quick call state for display so the map colours respond to the slider.
      rows = waveData.races.map((r) => {
        const p = r.forecast_d
        let call_state = 'tossup'
        if (p == null) call_state = 'unknown'
        else if (p >= 0.90) call_state = 'called_d'
        else if (p <= 0.10) call_state = 'called_r'
        else if (p >= 0.65) call_state = 'lean_d'
        else if (p <= 0.35) call_state = 'lean_r'
        return { ...r, call_state }
      })
    }
    const map = {}
    for (const r of rows) {
      if ((r.race_type || '').toLowerCase() !== chamber) continue
      const st = (r.state || '').toUpperCase()
      if (!st) continue
      if (!map[st] || (r.call_state !== 'unknown')) {
        map[st] = r
      }
    }
    return map
  }, [data, conditional, waveData, chamber])

  // Debounced wave fetch — re-runs when the slider settles.
  useEffect(() => {
    if (waveSwing === 0) {
      setWaveData(null)
      return
    }
    const t = setTimeout(() => {
      api.forecastWave?.(waveSwing).then(setWaveData).catch(() => setWaveData(null))
    }, 150)
    return () => clearTimeout(t)
  }, [waveSwing])

  async function applyCondition(raceKey, outcome) {
    setConditionedKey(raceKey)
    setConditionedOutcome(outcome)
    const r = await api.forecastConditional(`${raceKey}=${outcome}`).catch(() => null)
    setConditional(r)
  }
  function clearCondition() {
    setConditional(null)
    setConditionedKey(null)
    setConditionedOutcome(null)
  }

  const activeRace = hovered || selected
  const conditionedState = useMemo(() => {
    if (!conditionedKey) return null
    const parts = conditionedKey.split('_')
    return parts.length > 1 ? parts[1] : null
  }, [conditionedKey])

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-3 mb-5">
        <div>
          <h1 className="text-3xl font-semibold text-stone-900 flex items-center gap-2">
            <Sparkles className="h-6 w-6 text-amber-500" />
            Map
          </h1>
          <p className="text-sm text-stone-500 mt-1">
            Hover a state for its forecast. Click "if D wins" or "if R wins" to see how
            every other race shifts under our common-factor swing model.
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {CHAMBER_OPTIONS.map((c) => (
            <button
              key={c.key}
              onClick={() => { setChamber(c.key); clearCondition() }}
              className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                chamber === c.key
                  ? 'bg-stone-900 text-white font-medium'
                  : 'bg-stone-50 text-stone-600 hover:bg-stone-100'
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>

      {jointSummary?.[chamber] && jointSummary[chamber].chamber_total > 0 && (
        <div className="bg-stone-900 text-white rounded-xl p-5 mb-4 grid grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] text-stone-400 uppercase tracking-wider">Expected D</div>
            <div className="text-3xl font-bold text-blue-300 tabular-nums">{jointSummary[chamber].expected_d}</div>
          </div>
          <div>
            <div className="text-[10px] text-stone-400 uppercase tracking-wider">Expected R</div>
            <div className="text-3xl font-bold text-rose-300 tabular-nums">{jointSummary[chamber].expected_r}</div>
          </div>
          <div>
            <div className="text-[10px] text-stone-400 uppercase tracking-wider">Of</div>
            <div className="text-3xl font-bold tabular-nums">{jointSummary[chamber].chamber_total}</div>
            <div className="text-[10px] text-stone-500 mt-0.5">Monte Carlo · {jointSummary[chamber].n_samples} swing draws</div>
          </div>
        </div>
      )}

      {/* Wave-election scenario slider. When non-zero it recolours the map
          to show the implied calls under a national swing. Conditional view
          takes precedence if active. */}
      <div className="bg-stone-900 text-white rounded-xl p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs uppercase tracking-wider text-stone-400">
            Wave scenario {waveSwing !== 0 && (conditional?.available ? '(overridden by conditional)' : '')}
          </div>
          <div className="text-xs tabular-nums">
            {waveSwing === 0 ? 'neutral' : (waveSwing > 0 ? `D+${waveSwing.toFixed(1)}pp` : `R+${Math.abs(waveSwing).toFixed(1)}pp`)}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-rose-300 text-xs">R+10</span>
          <input
            type="range"
            min={-10}
            max={10}
            step={0.5}
            value={waveSwing}
            onChange={(e) => setWaveSwing(parseFloat(e.target.value))}
            className="flex-1 accent-amber-400"
            disabled={!!conditional?.available}
          />
          <span className="text-blue-300 text-xs">D+10</span>
          {waveSwing !== 0 && (
            <button
              onClick={() => setWaveSwing(0)}
              className="text-[10px] text-stone-400 hover:text-stone-200 underline"
            >
              reset
            </button>
          )}
        </div>
        {waveData?.chambers?.[chamber] && (
          <div className="text-[10px] text-stone-400 mt-2 tabular-nums">
            Under this swing · {chamber}: D wins {waveData.chambers[chamber].d} / R wins {waveData.chambers[chamber].r}
            {' · '}expected D {waveData.chambers[chamber].expected_d}
          </div>
        )}
      </div>

      {conditional?.available && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 mb-4 flex items-center gap-3 text-sm">
          <Sparkles className="h-4 w-4 text-amber-600 flex-shrink-0" />
          <div className="flex-1">
            <span className="font-semibold text-amber-900">Conditional view:</span>{' '}
            <span className="text-amber-800">
              if <strong>{conditioned.race_key_display(conditional)}</strong> resolves
              for <strong>{conditional.conditioned.outcome}</strong>, the map shows the implied shift.
            </span>
          </div>
          <button
            onClick={clearCondition}
            className="text-xs flex items-center gap-1 px-2 py-1 rounded bg-white border border-amber-300 text-amber-700 hover:bg-amber-100"
          >
            <RotateCcw className="h-3 w-3" />
            Clear
          </button>
        </div>
      )}

      <div className="grid lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 bg-white shadow-sm border border-stone-100 rounded-xl p-5">
          <StateGridMap
            racesByState={racesByState}
            onHover={(r) => setHovered(r)}
            onClick={(r) => setSelected(r)}
            selectedState={(selected?.state || '').toUpperCase()}
            conditionedState={conditionedState}
          />
        </div>

        <RaceSidebar
          race={activeRace}
          conditioned={conditional?.conditioned}
          onCondition={applyCondition}
          onClear={clearCondition}
          conditioning={conditionedKey != null}
        />
      </div>
    </div>
  )
}

const conditioned = {
  race_key_display: (cond) => {
    if (!cond?.conditioned?.race_key) return ''
    return cond.conditioned.race_key.replace(/_/g, ' · ')
  },
}

function RaceSidebar({ race, conditioned, onCondition, onClear, conditioning }) {
  if (!race) {
    return (
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
        <p className="text-sm text-stone-400 text-center py-10">
          Hover or click a state to see its forecast and conditional view.
        </p>
      </div>
    )
  }
  const lean = race.forecast_d >= 0.5 ? 'D' : 'R'
  const leanPct = (lean === 'D' ? race.forecast_d : 1 - race.forecast_d) * 100
  const sm = race.smart_money || {}
  const isConditioned = conditioned?.race_key === race.race_key
  const delta = race.delta_pp

  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
      <div className="text-xs text-stone-400 uppercase tracking-wider mb-1">
        {race.race_type} · {race.state}
      </div>
      <div className="font-semibold text-stone-900 mb-2 truncate">
        {race.race_key?.replace(/_/g, ' · ')}
      </div>

      <div className="flex items-baseline gap-2">
        <span
          className="text-3xl font-bold tabular-nums"
          style={{ color: lean === 'D' ? '#2563eb' : '#dc2626' }}
        >
          {lean}
        </span>
        <span className="text-3xl font-bold tabular-nums">{leanPct.toFixed(1)}%</span>
      </div>
      {delta != null && Math.abs(delta) >= 0.1 && (
        <div
          className="mt-1 inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full"
          style={{
            color: delta > 0 ? '#1d4ed8' : '#b91c1c',
            background: delta > 0 ? '#dbeafe' : '#fee2e2',
          }}
          title="Shift vs unconditional forecast"
        >
          conditional Δ {delta > 0 ? '+' : ''}{delta.toFixed(1)}pp
        </div>
      )}

      <div className="mt-3 text-xs text-stone-500">
        {race.call_label}
        {race.n_sources != null && ` · ${race.n_sources} sources`}
      </div>

      {sm.available && sm.direction && (
        <div className="mt-3 p-2 rounded bg-stone-50 border border-stone-100 text-xs">
          <div className="flex items-center gap-1 text-stone-500 uppercase tracking-wider text-[10px]">
            <Wallet className="h-3 w-3" />
            Smart money
          </div>
          <div className="mt-1 flex items-center gap-2">
            <span className="font-bold" style={{ color: sm.direction === 'D' ? '#2563eb' : '#dc2626' }}>
              {sm.direction} {(sm.lean_strength * 100).toFixed(0)}%
            </span>
            <span className="text-stone-500">{fmtUsd(sm.total_smart_usd)} · {sm.smart_wallet_count} wallets</span>
            {sm.direction !== lean && (
              <AlertTriangle className="h-3 w-3 text-amber-500 ml-auto" />
            )}
          </div>
        </div>
      )}

      <div className="mt-4 border-t border-stone-100 pt-3">
        <div className="text-[10px] uppercase tracking-wider text-stone-500 mb-2">
          {conditioning ? 'Re-condition the map' : 'What if…'}
        </div>
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={() => onCondition(race.race_key, 'D')}
            disabled={isConditioned && conditioned.outcome === 'D'}
            className="text-xs font-semibold px-3 py-2 rounded-md border border-blue-200 text-blue-700 bg-blue-50 hover:bg-blue-100 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            If D wins
          </button>
          <button
            onClick={() => onCondition(race.race_key, 'R')}
            disabled={isConditioned && conditioned.outcome === 'R'}
            className="text-xs font-semibold px-3 py-2 rounded-md border border-rose-200 text-rose-700 bg-rose-50 hover:bg-rose-100 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            If R wins
          </button>
        </div>
        {conditioning && (
          <button
            onClick={onClear}
            className="mt-2 w-full text-xs flex items-center justify-center gap-1 px-3 py-1.5 rounded-md text-stone-600 hover:bg-stone-100"
          >
            <RotateCcw className="h-3 w-3" />
            Clear conditional
          </button>
        )}
      </div>

      <Link
        to={`/race/${race.race_key}`}
        className="mt-4 inline-flex items-center gap-1 text-xs text-stone-600 hover:text-stone-900"
      >
        Open race detail
        <ArrowRight className="h-3 w-3" />
      </Link>
    </div>
  )
}
