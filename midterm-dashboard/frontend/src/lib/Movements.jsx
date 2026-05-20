import React, { useState, useEffect } from 'react'
import { api } from './api'
import { TrendingUp, TrendingDown, Newspaper, AlertCircle } from 'lucide-react'

const SOURCE_COLORS = { polymarket: '#8b5cf6', kalshi: '#3b82f6', predictit: '#f59e0b', polling: '#10b981' }

export default function Movements({ raceKey, hours = 24 }) {
  const [data, setData] = useState({ movements: [], candidates: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!raceKey) return
    setLoading(true)
    api.movements(raceKey, hours)
      .then(setData)
      .catch(e => setError(e.message || 'Failed to load'))
      .finally(() => setLoading(false))
  }, [raceKey, hours])

  if (loading) return null
  if (error) return null
  if (!data.movements?.length && !data.candidates?.length) return null

  return (
    <section aria-labelledby="why-moved-heading"
      className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 sm:p-6 mb-6">
      <h3 id="why-moved-heading" className="text-lg font-semibold text-stone-800 flex items-center gap-2 mb-4">
        <TrendingUp className="h-5 w-5 text-stone-500" aria-hidden="true" />
        Why did this move? <span className="text-xs font-normal text-stone-400">last {hours}h</span>
      </h3>

      {data.movements?.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-4">
          {data.movements.map(m => {
            const isUp = m.delta_pp >= 0
            const color = SOURCE_COLORS[m.source] || '#78716c'
            return (
              <div key={m.source} className="flex items-center justify-between p-2.5 border border-stone-100 rounded-lg">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold uppercase tracking-wide" style={{ color }}>{m.source}</span>
                </div>
                <div className="flex items-center gap-1 text-sm">
                  <span className="tabular-nums text-stone-500">{(m.from * 100).toFixed(0)}%</span>
                  <span className="text-stone-300">→</span>
                  <span className="tabular-nums font-semibold text-stone-800">{(m.to * 100).toFixed(0)}%</span>
                  <span className={`tabular-nums font-bold ml-2 inline-flex items-center gap-0.5 ${isUp ? 'text-emerald-600' : 'text-red-600'}`}>
                    {isUp ? <TrendingUp className="h-3 w-3" aria-hidden="true" /> : <TrendingDown className="h-3 w-3" aria-hidden="true" />}
                    {isUp ? '+' : ''}{m.delta_pp.toFixed(1)}pp
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {data.candidates?.length > 0 && (
        <div className="space-y-1.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1">
            <Newspaper className="h-3 w-3" aria-hidden="true" />Candidate explanations
          </div>
          {data.candidates.map((c, i) => (
            <div key={i} className="text-xs text-stone-600 bg-stone-50 border border-stone-100 rounded-md p-2.5 flex items-start gap-2">
              <AlertCircle className="h-3.5 w-3.5 text-stone-400 shrink-0 mt-0.5" aria-hidden="true" />
              <div>
                <div className="font-medium text-stone-700">{c.headline}</div>
                {c.note && <div className="text-stone-500 mt-0.5">{c.note}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
