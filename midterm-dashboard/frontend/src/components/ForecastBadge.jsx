import React, { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { sourceColors, sourceLabels } from '../lib/raceTheme.jsx'
import { Sparkles, ChevronDown, ChevronUp, Wallet, AlertTriangle } from 'lucide-react'

// Format dollar amounts compactly: $1.2M / $340k / $890
function fmtUsd(usd) {
  const n = Number(usd) || 0
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}k`
  return `$${Math.round(n)}`
}

// The signature narve.ai forecast badge — a single ensemble probability with
// a confidence chip, expandable to show source weights. Use anywhere a race
// is displayed in detail (RaceDetail page, eventually a popover on hover).
export default function ForecastBadge({ raceKey, compact = false }) {
  const [forecast, setForecast] = useState(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!raceKey) return
    setLoading(true)
    api.forecast(raceKey)
      .then(setForecast)
      .catch(() => setForecast(null))
      .finally(() => setLoading(false))
  }, [raceKey])

  if (loading) {
    return (
      <div className={compact ? 'h-5 w-20 bg-stone-100 rounded animate-pulse' : 'h-16 bg-stone-100 rounded-xl animate-pulse'} />
    )
  }
  if (!forecast || forecast.forecast_d == null) return null

  const pct = (forecast.forecast_d * 100).toFixed(1)
  // forecast_d is P(Democrat); if > 0.5 lean is D, else R.
  const lean = forecast.forecast_d >= 0.5 ? 'D' : 'R'
  const leanPct = (lean === 'D' ? forecast.forecast_d : 1 - forecast.forecast_d) * 100
  const leanColor = lean === 'D' ? '#3b82f6' : '#ef4444'
  const conf = forecast.confidence ?? 0
  const confLabel = conf >= 0.75 ? 'High' : conf >= 0.45 ? 'Medium' : 'Low'
  const confColor = conf >= 0.75 ? 'bg-emerald-50 text-emerald-700' : conf >= 0.45 ? 'bg-amber-50 text-amber-700' : 'bg-stone-100 text-stone-500'

  if (compact) {
    return (
      <span
        className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium"
        style={{ background: `${leanColor}1A`, color: leanColor }}
        title={`narve.ai forecast: ${lean} ${leanPct.toFixed(1)}% — confidence ${confLabel}`}
      >
        <Sparkles className="h-3 w-3" />
        narve.ai · {lean} {leanPct.toFixed(0)}%
      </span>
    )
  }

  return (
    <div className="bg-gradient-to-br from-stone-900 to-stone-800 text-white rounded-xl p-5 shadow-lg">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-sm text-stone-300 font-medium tracking-wide">
          <Sparkles className="h-4 w-4 text-amber-300" />
          narve.ai forecast
        </div>
        <span className={`text-[10px] px-2 py-0.5 rounded-full font-semibold uppercase tracking-wider ${confColor}`}>
          {confLabel} confidence
        </span>
      </div>

      <div className="flex items-baseline gap-3 mt-1">
        <span className="text-3xl font-bold tabular-nums" style={{ color: leanColor }}>
          {lean}
        </span>
        <span className="text-3xl font-bold tabular-nums">{leanPct.toFixed(1)}%</span>
        <span className="text-xs text-stone-400 self-end pb-1">
          P(D) = {pct}%
        </span>
      </div>

      <div className="mt-3 h-2 bg-stone-700 rounded-full overflow-hidden flex">
        <div className="bg-blue-500" style={{ width: `${forecast.forecast_d * 100}%` }} />
        <div className="bg-rose-500 flex-1" />
      </div>
      <div className="flex justify-between text-[10px] text-stone-400 mt-1.5 tabular-nums">
        <span>D {(forecast.forecast_d * 100).toFixed(1)}%</span>
        <span>R {((1 - forecast.forecast_d) * 100).toFixed(1)}%</span>
      </div>

      {/* Smart-money signal — proven-quality wallet positioning for this race.
          When the forecast disagrees with the smart-money lean, the divergence
          is highlighted in amber as a "smart money divergence". */}
      {forecast.smart_money?.available && forecast.smart_money.direction && (() => {
        const sm = forecast.smart_money
        const smColor = sm.direction === 'D' ? '#3b82f6' : '#ef4444'
        const diverges = sm.direction !== lean
        return (
          <div className={`mt-3 rounded-lg p-2.5 ${diverges ? 'bg-amber-500/10 border border-amber-500/30' : 'bg-stone-700/40 border border-stone-700'}`}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-stone-300">
                <Wallet className="h-3 w-3" />
                Smart money
                {diverges && (
                  <span className="inline-flex items-center gap-1 text-amber-300 normal-case tracking-normal text-[10px] font-semibold">
                    <AlertTriangle className="h-3 w-3" />
                    diverges
                  </span>
                )}
              </div>
              <span className="text-[10px] text-stone-400">
                {sm.smart_wallet_count} {sm.smart_wallet_count === 1 ? 'wallet' : 'wallets'}
              </span>
            </div>
            <div className="flex items-baseline gap-2 mt-1">
              <span className="text-base font-bold tabular-nums" style={{ color: smColor }}>
                {sm.direction}
              </span>
              <span className="text-base font-bold tabular-nums">
                {(sm.lean_strength * 100).toFixed(0)}%
              </span>
              <span className="text-xs text-stone-400 ml-auto tabular-nums">
                {fmtUsd(sm.total_smart_usd)} positioned
              </span>
            </div>
          </div>
        )
      })()}

      <button
        onClick={() => setExpanded((v) => !v)}
        className="mt-3 flex items-center gap-1 text-xs text-stone-400 hover:text-stone-200 transition-colors"
      >
        {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        {forecast.n_sources} {forecast.n_sources === 1 ? 'source' : 'sources'}
        {forecast.method === 'brier_weighted' ? ' · Brier-weighted' : ' · prior weights'}
      </button>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-stone-700 space-y-1.5">
          {forecast.sources_used.map((src) => {
            const p = forecast.source_probs[src]
            const w = forecast.weights[src]
            return (
              <div key={src} className="flex items-center gap-2 text-xs">
                <span
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ backgroundColor: sourceColors[src] || '#78716c' }}
                />
                <span className="w-24 text-stone-300">{sourceLabels[src] || src}</span>
                <span className="flex-1 tabular-nums text-stone-200">{(p * 100).toFixed(1)}%</span>
                <span className="text-stone-400 tabular-nums">w={w.toFixed(2)}</span>
              </div>
            )
          })}
          {forecast.spread != null && (
            <div className="text-[10px] text-stone-500 mt-2">
              Source spread: {(forecast.spread * 100).toFixed(1)}pp
            </div>
          )}
        </div>
      )}
    </div>
  )
}
