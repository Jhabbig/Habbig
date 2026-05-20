import React, { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { Target, TrendingDown, TrendingUp, Info } from 'lucide-react'

const SOURCE_COLORS = {
  polymarket: '#8b5cf6', kalshi: '#3b82f6', predictit: '#f59e0b', polling: '#10b981',
}

function StatCell({ value, kind }) {
  if (value == null) return <span className="text-stone-300">—</span>
  if (kind === 'percent') {
    const v = Math.round(value * 100)
    const color = v >= 85 ? 'text-emerald-600' : v >= 70 ? 'text-amber-600' : 'text-red-600'
    return <span className={`tabular-nums font-semibold ${color}`}>{v}%</span>
  }
  if (kind === 'brier') {
    // 0 = perfect, 0.25 = coinflip; under 0.10 is excellent, over 0.20 is poor
    const color = value <= 0.10 ? 'text-emerald-600' : value <= 0.18 ? 'text-amber-600' : 'text-red-600'
    return <span className={`tabular-nums font-semibold ${color}`}>{value.toFixed(3)}</span>
  }
  return <span className="tabular-nums">{value}</span>
}

function StatsTable({ stats, title, subtitle }) {
  const sources = Object.entries(stats || {}).sort((a, b) => (b[1].n || 0) - (a[1].n || 0))
  if (sources.length === 0) return null
  return (
    <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-4">
      <div className="flex items-baseline justify-between mb-3 flex-wrap gap-2">
        <h2 className="text-sm font-semibold text-stone-800">{title}</h2>
        {subtitle && <span className="text-xs text-stone-400">{subtitle}</span>}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-[11px] text-stone-400 uppercase tracking-wide">
            <tr className="border-b border-stone-100">
              <th scope="col" className="text-left py-2">Source</th>
              <th scope="col" className="text-right py-2">N races</th>
              <th scope="col" className="text-right py-2">Hit rate</th>
              <th scope="col" className="text-right py-2">Brier</th>
              <th scope="col" className="text-right py-2">Toss-up calibration</th>
            </tr>
          </thead>
          <tbody>
            {sources.map(([src, s]) => (
              <tr key={src} className="border-t border-stone-50">
                <td className="py-2">
                  <span className="inline-flex items-center gap-2">
                    <span className="w-2 h-2 rounded-full" style={{ backgroundColor: SOURCE_COLORS[src] || '#78716c' }} aria-hidden="true" />
                    <span className="capitalize font-medium text-stone-800">{src}</span>
                  </span>
                </td>
                <td className="py-2 text-right tabular-nums text-stone-600">{s.n}</td>
                <td className="py-2 text-right"><StatCell value={s.hit_rate} kind="percent" /></td>
                <td className="py-2 text-right"><StatCell value={s.brier} kind="brier" /></td>
                <td className="py-2 text-right">
                  <StatCell value={s.calibration_50} kind="percent" />
                  {s.n_toss_ups != null && (
                    <span className="text-[10px] text-stone-400 ml-1">(n={s.n_toss_ups})</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export default function Accuracy() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.accuracy()
      .then(setData)
      .catch(e => setError(e.message || 'Failed to load accuracy stats'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div role="status" aria-live="polite" className="text-stone-400 text-sm">
        Loading accuracy stats…
      </div>
    )
  }
  if (error) {
    return (
      <div role="alert" className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
        {error}
      </div>
    )
  }
  if (!data?.summary) return null

  const { summary, methodology } = data
  const byRaceType = summary.by_race_type || {}

  return (
    <div>
      <div className="flex items-center gap-3 mb-6 flex-wrap">
        <div className="p-2 bg-emerald-50 rounded-lg">
          <Target className="h-6 w-6 text-emerald-600" aria-hidden="true" />
        </div>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl font-bold text-stone-900">Source accuracy track record</h1>
          <p className="text-stone-500 text-sm">
            How well each market and polling source predicted past US elections.{' '}
            <span className="text-stone-400">{summary.race_count} resolved races, {summary.prediction_count} predictions.</span>
          </p>
        </div>
      </div>

      <div className="grid sm:grid-cols-3 gap-2 mb-4 text-xs text-stone-600">
        <div className="bg-emerald-50/60 border border-emerald-100 rounded-lg p-2.5 flex items-start gap-2">
          <TrendingUp className="h-3.5 w-3.5 text-emerald-600 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <div className="font-semibold text-emerald-800">Hit rate</div>
            <div>Source picked the right winner (assigned ≥50% to the eventual winner).</div>
          </div>
        </div>
        <div className="bg-amber-50/60 border border-amber-100 rounded-lg p-2.5 flex items-start gap-2">
          <TrendingDown className="h-3.5 w-3.5 text-amber-600 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <div className="font-semibold text-amber-800">Brier score</div>
            <div>Mean (1 - prob)². 0 = perfect, 0.25 = coinflip. Lower is better.</div>
          </div>
        </div>
        <div className="bg-stone-50 border border-stone-200 rounded-lg p-2.5 flex items-start gap-2">
          <Info className="h-3.5 w-3.5 text-stone-500 shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <div className="font-semibold text-stone-700">Toss-up calibration</div>
            <div>Among 40–60% predictions, fraction that won. Well-calibrated sources land near 50%.</div>
          </div>
        </div>
      </div>

      <StatsTable
        stats={summary.overall}
        title="Overall accuracy (all cycles, all race types)"
        subtitle="2020–2024"
      />

      {Object.entries(byRaceType).map(([rt, s]) => (
        <StatsTable
          key={rt}
          stats={s}
          title={`${rt[0].toUpperCase()}${rt.slice(1)} races`}
          subtitle={`${rt} only`}
        />
      ))}

      <StatsTable
        stats={summary.since_2024}
        title="Since 2024"
        subtitle="Most recent cycle"
      />

      {methodology && (
        <details className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mt-2 text-xs text-stone-600">
          <summary className="cursor-pointer text-stone-700 font-semibold">Methodology &amp; data provenance</summary>
          <div className="mt-3 space-y-2">
            {Object.entries(methodology.metrics || {}).map(([k, v]) => (
              <div key={k}><span className="font-semibold capitalize">{k.replace('_', ' ')}: </span>{v}</div>
            ))}
            {methodology.data_provenance && (
              <div className="mt-2 pt-2 border-t border-stone-100">{methodology.data_provenance}</div>
            )}
          </div>
        </details>
      )}
    </div>
  )
}
