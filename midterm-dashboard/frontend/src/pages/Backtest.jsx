import React, { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { sourceColors, sourceLabels } from '../lib/raceTheme.jsx'
import { ScatterChart, Scatter, XAxis, YAxis, ZAxis, ResponsiveContainer, Tooltip, CartesianGrid, ReferenceLine } from 'recharts'

const WINDOWS = [
  { label: '7 days', value: 7 },
  { label: '30 days', value: 30 },
  { label: '90 days', value: 90 },
  { label: '180 days', value: 180 },
]

export default function Backtest() {
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    api.backtest(days)
      .then(setData)
      .catch((e) => setError(e.message || String(e)))
      .finally(() => setLoading(false))
  }, [days])

  return (
    <div className="space-y-6">
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-stone-900">Source backtest</h1>
            <p className="text-sm text-stone-500 mt-1">
              Per-source Brier score on resolved races. Lower is better — perfect calibration is 0,
              random is 0.25. Coverage of 2026 races will grow as those races resolve.
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            {WINDOWS.map((w) => (
              <button
                key={w.value}
                onClick={() => setDays(w.value)}
                className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                  days === w.value
                    ? 'bg-stone-900 text-white font-medium'
                    : 'bg-stone-50 text-stone-600 hover:bg-stone-100'
                }`}
              >
                {w.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {loading && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-12 text-center text-stone-400">
          Loading backtest…
        </div>
      )}
      {error && (
        <div className="bg-rose-50 border border-rose-200 rounded-xl p-4 text-sm text-rose-700">
          {error}
        </div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
              <h2 className="text-sm font-semibold text-stone-800 mb-3">Brier score</h2>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-stone-400">
                    <th className="font-normal pb-2">Source</th>
                    <th className="font-normal pb-2 text-right">Brier</th>
                    <th className="font-normal pb-2 text-right">Resolved</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.brier || {})
                    .sort((a, b) => {
                      const av = a[1] ?? Infinity
                      const bv = b[1] ?? Infinity
                      return av - bv
                    })
                    .map(([src, brier]) => {
                      const resolved = data.coverage?.[src]?.resolved_races ?? 0
                      return (
                        <tr key={src} className="border-t border-stone-100">
                          <td className="py-2 flex items-center gap-2">
                            <span
                              className="inline-block w-2.5 h-2.5 rounded-full"
                              style={{ backgroundColor: sourceColors[src] || '#78716c' }}
                            />
                            <span className="font-medium text-stone-700">
                              {sourceLabels[src] || src}
                            </span>
                          </td>
                          <td className="py-2 text-right tabular-nums font-bold text-stone-800">
                            {brier == null ? '—' : brier.toFixed(4)}
                          </td>
                          <td className="py-2 text-right tabular-nums text-stone-500">
                            {resolved}
                          </td>
                        </tr>
                      )
                    })}
                </tbody>
              </table>
            </div>

            <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
              <h2 className="text-sm font-semibold text-stone-800 mb-3">Coverage</h2>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-stone-400">
                    <th className="font-normal pb-2">Source</th>
                    <th className="font-normal pb-2 text-right">Snapshots</th>
                    <th className="font-normal pb-2 text-right">Races</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(data.coverage || {}).map(([src, c]) => (
                    <tr key={src} className="border-t border-stone-100">
                      <td className="py-2 flex items-center gap-2">
                        <span
                          className="inline-block w-2.5 h-2.5 rounded-full"
                          style={{ backgroundColor: sourceColors[src] || '#78716c' }}
                        />
                        <span className="font-medium text-stone-700">
                          {sourceLabels[src] || src}
                        </span>
                      </td>
                      <td className="py-2 text-right tabular-nums">{c.snapshots ?? 0}</td>
                      <td className="py-2 text-right tabular-nums">{c.races ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-xs text-stone-400 mt-3">
                Total snapshots in window: {data.snapshots_total}
              </p>
            </div>
          </div>

          <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
            <h2 className="text-sm font-semibold text-stone-800 mb-3">Calibration scatter</h2>
            <p className="text-xs text-stone-400 mb-4">
              Each dot is a snapshot of a resolved race. X = source's P(D), Y = realized outcome
              (0 = R won, 1 = D won). A perfectly-calibrated source clusters near the diagonal.
            </p>
            <div style={{ width: '100%', height: 320 }}>
              <ResponsiveContainer>
                <ScatterChart>
                  <CartesianGrid stroke="#e7e5e4" strokeDasharray="3 3" />
                  <XAxis
                    type="number"
                    dataKey="prob_d"
                    name="P(D)"
                    domain={[0, 1]}
                    tickFormatter={(v) => `${(v * 100).toFixed(0)}%`}
                  />
                  <YAxis type="number" dataKey="outcome" domain={[0, 1]} ticks={[0, 1]} />
                  <ZAxis range={[40, 40]} />
                  <Tooltip cursor={{ strokeDasharray: '3 3' }} />
                  <ReferenceLine
                    segment={[
                      { x: 0, y: 0 },
                      { x: 1, y: 1 },
                    ]}
                    stroke="#a8a29e"
                    strokeDasharray="4 4"
                  />
                  {Object.keys(sourceColors).map((src) => {
                    const samples = (data.samples || [])
                      .filter((s) => s.source === src)
                      .map((s) => ({ ...s, outcome: s.winner === 'D' ? 1 : 0 }))
                    if (!samples.length) return null
                    return (
                      <Scatter
                        key={src}
                        name={sourceLabels[src] || src}
                        data={samples}
                        fill={sourceColors[src]}
                      />
                    )
                  })}
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
