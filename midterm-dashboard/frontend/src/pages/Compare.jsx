import React, { useState, useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { GitCompare, ArrowRight, ArrowUpDown, Download } from 'lucide-react'

const SOURCE_BADGES = {
  polymarket: 'bg-purple-100 text-purple-700',
  kalshi: 'bg-blue-100 text-blue-700',
  predictit: 'bg-amber-100 text-amber-700',
  polling: 'bg-emerald-100 text-emerald-700',
  manifold: 'bg-pink-100 text-pink-700',
  metaculus: 'bg-cyan-100 text-cyan-700',
}

const SOURCE_ORDER = ['polymarket', 'kalshi', 'predictit', 'polling', 'manifold', 'metaculus']

function pct(p) {
  if (p == null) return null
  return Math.round((typeof p === 'number' && p <= 1 ? p * 100 : p))
}

function sourceCell(prob) {
  const v = pct(prob)
  if (v == null) return <span className="text-stone-300 text-xs">—</span>
  return <span className="tabular-nums font-medium text-stone-800">{v}%</span>
}

export default function Compare() {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [sortKey, setSortKey] = useState('spread')
  const [sortDir, setSortDir] = useState('desc')

  useEffect(() => {
    api.comparison()
      .then(d => setRows(d?.rows || []))
      .catch(e => setError(e.message || 'Failed to load comparison'))
      .finally(() => setLoading(false))
  }, [])

  const sorted = useMemo(() => {
    const dir = sortDir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      const av = a[sortKey] ?? -Infinity
      const bv = b[sortKey] ?? -Infinity
      if (typeof av === 'string') return av.localeCompare(bv) * dir
      return ((av || 0) - (bv || 0)) * dir
    })
  }, [rows, sortKey, sortDir])

  function toggleSort(key) {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  function SortHeader({ k, children, className }) {
    const active = sortKey === k
    return (
      <th scope="col" tabIndex={0}
        onClick={() => toggleSort(k)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleSort(k) } }}
        aria-sort={active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
        className={`text-left text-[11px] font-semibold text-stone-500 uppercase tracking-wide cursor-pointer select-none px-3 py-2 ${className || ''}`}>
        <span className="inline-flex items-center gap-1">{children}<ArrowUpDown className={`h-3 w-3 ${active ? 'text-stone-700' : 'text-stone-300'}`} aria-hidden="true" /></span>
      </th>
    )
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6 gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <GitCompare className="h-6 w-6 text-emerald-600" aria-hidden="true" />
          <h1 className="text-2xl sm:text-3xl font-semibold text-stone-800">Cross-source comparison</h1>
        </div>
        <a href={api.exportRacesCsvUrl()}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs bg-stone-100 text-stone-700 hover:bg-stone-200 transition-colors"
          aria-label="Download comparison as CSV">
          <Download className="h-3.5 w-3.5" aria-hidden="true" /> CSV
        </a>
      </div>

      <p className="text-sm text-stone-500 mb-4">
        Side-by-side top-outcome probabilities for every race tracked by 2+ sources. Sort by any column to spot the biggest divergences.
      </p>

      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      {loading ? (
        <div role="status" aria-live="polite" className="bg-white shadow-sm border border-stone-100 rounded-xl animate-pulse h-96">
          <span className="sr-only">Loading comparison…</span>
        </div>
      ) : (
        <>
          {/* Desktop: table */}
          <div className="hidden md:block bg-white shadow-sm border border-stone-100 rounded-xl overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-stone-50 border-b border-stone-100">
                  <tr>
                    <SortHeader k="title">Race</SortHeader>
                    <SortHeader k="state">State</SortHeader>
                    {SOURCE_ORDER.map(s => (
                      <SortHeader key={s} k={s}>{s}</SortHeader>
                    ))}
                    <SortHeader k="spread">Spread</SortHeader>
                    <th scope="col" className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.length === 0 ? (
                    <tr><td colSpan={SOURCE_ORDER.length + 4} className="text-center py-10 text-stone-400">No multi-source races available.</td></tr>
                  ) : sorted.map(r => (
                    <tr key={r.race_key} className="border-b border-stone-50 hover:bg-stone-50/50">
                      <td className="px-3 py-2.5 max-w-md truncate" title={r.title}>{r.title}</td>
                      <td className="px-3 py-2.5">
                        {r.state ? <span className="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded font-medium">{r.state}</span> : <span className="text-stone-300">—</span>}
                      </td>
                      {SOURCE_ORDER.map(s => (
                        <td key={s} className="px-3 py-2.5">{sourceCell(r[s])}</td>
                      ))}
                      <td className="px-3 py-2.5">
                        {r.spread != null && (
                          <span className={`tabular-nums font-bold ${r.spread > 10 ? 'text-red-600' : r.spread > 5 ? 'text-amber-600' : 'text-emerald-600'}`}>{r.spread.toFixed(1)}%</span>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        <Link to={`/race/${r.race_key}`} aria-label={`Open ${r.title}`} className="text-stone-400 hover:text-stone-700">
                          <ArrowRight className="h-4 w-4" aria-hidden="true" />
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Mobile: stacked cards */}
          <div className="md:hidden grid gap-3">
            {sorted.map(r => (
              <Link to={`/race/${r.race_key}`} key={r.race_key}
                className="bg-white shadow-sm border border-stone-100 rounded-xl p-4 block hover:border-stone-300 transition-colors">
                <div className="flex items-center justify-between mb-2 gap-2">
                  <span className="font-medium text-stone-800 text-sm truncate">{r.title}</span>
                  {r.state && <span className="text-[10px] bg-stone-100 text-stone-600 px-1.5 py-0.5 rounded font-medium shrink-0">{r.state}</span>}
                </div>
                <div className="grid grid-cols-2 gap-2">
                  {SOURCE_ORDER.filter(s => r[s] != null).map(s => (
                    <div key={s} className="flex items-center justify-between text-xs">
                      <span className={`px-1.5 py-0.5 rounded font-medium ${SOURCE_BADGES[s] || 'bg-stone-100 text-stone-600'}`}>{s}</span>
                      <span className="tabular-nums font-medium text-stone-800">{pct(r[s])}%</span>
                    </div>
                  ))}
                </div>
                {r.spread != null && (
                  <div className="text-[11px] text-stone-500 mt-2 pt-2 border-t border-stone-100">
                    Spread: <span className={`tabular-nums font-bold ${r.spread > 10 ? 'text-red-600' : r.spread > 5 ? 'text-amber-600' : 'text-emerald-600'}`}>{r.spread.toFixed(1)}%</span>
                  </div>
                )}
              </Link>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
