import React, { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { GitCompare, ArrowRight, AlertTriangle } from 'lucide-react'

export default function Divergence() {
  const [divergences, setDivergences] = useState([])
  const [loading, setLoading] = useState(true)
  const [sortBy, setSortBy] = useState('divergence')

  useEffect(() => {
    api.divergence().then(data => setDivergences(Array.isArray(data) ? data : data?.divergences || []))
      .catch(() => {}).finally(() => setLoading(false))
  }, [])

  const sorted = [...divergences].sort((a, b) => {
    if (sortBy === 'divergence') return (b.max_divergence || 0) - (a.max_divergence || 0)
    return (a.race_key || '').localeCompare(b.race_key || '')
  })

  const chartData = sorted.slice(0, 15).map(d => ({
    name: d.race_key?.replace('_', ' ') || '?',
    divergence: (d.max_divergence || 0) * 100,
  }))

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-3xl font-semibold text-stone-800 flex items-center gap-2">
          <GitCompare className="h-7 w-7 text-amber-600" />Source Divergence
        </h1>
        <p className="text-stone-500 text-sm mt-1">Where prediction markets and polls disagree — the bigger the divergence, the more sources disagree.</p>
      </div>

      {chartData.length > 0 && (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 mb-6">
          <h3 className="text-sm font-semibold text-stone-500 mb-4 uppercase tracking-wide">Divergence by Race</h3>
          <ResponsiveContainer width="100%" height={Math.max(chartData.length * 35, 200)}>
            <BarChart data={chartData} layout="vertical" margin={{ left: 100 }}>
              <XAxis type="number" tick={{ fill: '#78716c', fontSize: 12 }} tickFormatter={v => `${v.toFixed(0)}%`} />
              <YAxis type="category" dataKey="name" tick={{ fill: '#78716c', fontSize: 12 }} width={100} />
              <Tooltip contentStyle={{ backgroundColor: '#ffffff', border: '1px solid #e7e5e4', borderRadius: '12px', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.05)' }} formatter={v => `${v.toFixed(1)}%`} />
              <Bar dataKey="divergence" radius={[0, 4, 4, 0]}>
                {chartData.map((d, i) => (
                  <Cell key={i} fill={d.divergence > 15 ? '#fb7185' : d.divergence > 8 ? '#fbbf24' : '#6ee7b7'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="flex gap-2 mb-4">
        {['divergence', 'name'].map(s => (
          <button key={s} onClick={() => setSortBy(s)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize ${sortBy === s ? 'bg-stone-100 text-stone-900' : 'bg-stone-50 text-stone-500 hover:bg-stone-100'}`}>
            By {s}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="space-y-3">{[1,2,3,4,5].map(i => <div key={i} className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-24"></div>)}</div>
      ) : sorted.length > 0 ? (
        <div className="grid gap-3">
          {sorted.map((d, i) => {
            const maxDiv = (d.max_divergence || 0) * 100
            const color = maxDiv > 15 ? 'text-rose-500 bg-rose-50' : maxDiv > 8 ? 'text-amber-600 bg-amber-50' : 'text-emerald-600 bg-emerald-50'
            return (
              <Link key={i} to={`/race/${d.race_key}`} className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 hover:border-stone-200 transition-colors">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-medium text-stone-800">{d.race_key?.replace('_', ' — ')}</div>
                    <div className="flex items-center gap-4 mt-2 text-xs text-stone-400">
                      {d.polymarket_prob != null && <span>Polymarket: <span className="text-blue-600">{(d.polymarket_prob * 100).toFixed(0)}%</span></span>}
                      {d.kalshi_prob != null && <span>Kalshi: <span className="text-rose-500">{(d.kalshi_prob * 100).toFixed(0)}%</span></span>}
                      {d.predictit_prob != null && <span>PredictIt: <span className="text-amber-600">{(d.predictit_prob * 100).toFixed(0)}%</span></span>}
                      {d.polling_avg != null && <span>Polls: <span className="text-emerald-600">{(d.polling_avg * 100).toFixed(0)}%</span></span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-3">
                    <span className={`px-3 py-1.5 rounded-lg font-bold text-sm ${color}`}>{maxDiv.toFixed(1)}%</span>
                    <ArrowRight className="h-4 w-4 text-stone-400" />
                  </div>
                </div>
              </Link>
            )
          })}
        </div>
      ) : (
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-6 text-center py-12">
          <AlertTriangle className="h-8 w-8 text-stone-400 mx-auto mb-3" />
          <p className="text-stone-400">No divergence data available yet.</p>
          <p className="text-stone-400 text-sm mt-1">Data refreshes every 5 minutes.</p>
        </div>
      )}
    </div>
  )
}
