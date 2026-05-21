import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { Activity, AlertTriangle, CheckCircle2, ExternalLink, Pause, Play, RefreshCw, Tv } from 'lucide-react'

const SOURCE_LABELS = {
  polymarket: 'Polymarket', kalshi: 'Kalshi', predictit: 'PredictIt',
  polling: 'Polling', manifold: 'Manifold', metaculus: 'Metaculus',
}
const PARTY_COLOR = {
  D: { bg: 'bg-blue-500/15', text: 'text-blue-300', solid: 'bg-blue-500', label: 'D' },
  R: { bg: 'bg-red-500/15', text: 'text-red-300', solid: 'bg-red-500', label: 'R' },
  I: { bg: 'bg-purple-500/15', text: 'text-purple-300', solid: 'bg-purple-500', label: 'I' },
}
const SEVERITY = {
  high: { ring: 'ring-red-500/60 shadow-red-500/20', badge: 'bg-red-500 text-white', label: 'Big disagreement' },
  medium: { ring: 'ring-amber-500/60 shadow-amber-500/10', badge: 'bg-amber-500 text-stone-900', label: 'Disagreement' },
  low: { ring: 'ring-amber-500/30', badge: 'bg-amber-500/30 text-amber-200', label: 'Mild disagreement' },
}

function timeAgo(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function pct(p) {
  if (p == null) return null
  const v = typeof p === 'number' && p <= 1 ? p * 100 : p
  return Math.round(v)
}

function RaceCard({ row }) {
  const sev = row.disagreements?.length > 0
    ? row.disagreements.reduce((acc, d) => {
        const order = { low: 1, medium: 2, high: 3 }
        return order[d.severity] > order[acc] ? d.severity : acc
      }, 'low')
    : null
  const sevStyle = sev ? SEVERITY[sev] : null
  const called = row.called
  const callParty = called?.called_party && PARTY_COLOR[called.called_party]

  const sources = Object.entries(row.by_source || {})

  return (
    <article
      className={`bg-stone-900 border border-stone-800 rounded-xl p-4 ring-2 ${sevStyle ? `${sevStyle.ring} shadow-lg` : 'ring-transparent'} transition-shadow`}
      aria-label={`${row.title} live status`}
    >
      <header className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-wider text-stone-500 mb-0.5">
            {row.race_type} {row.state ? `· ${row.state}` : ''}
          </div>
          <Link to={`/race/${row.race_key}`}
            className="text-base sm:text-lg font-semibold text-stone-100 hover:text-white truncate inline-flex items-center gap-1">
            {row.title}
            <ExternalLink className="h-3 w-3 text-stone-500" aria-hidden="true" />
          </Link>
        </div>
        {sevStyle && (
          <span className={`text-[10px] font-bold uppercase tracking-wide px-2 py-1 rounded ${sevStyle.badge} shrink-0`}>
            {sevStyle.label}
          </span>
        )}
      </header>

      {/* Call status */}
      <div className="mb-3 flex items-center gap-2">
        {called && callParty ? (
          <div className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg ${callParty.bg} border border-stone-800`}>
            <CheckCircle2 className={`h-4 w-4 ${callParty.text}`} aria-hidden="true" />
            <span className={`font-bold ${callParty.text}`}>{callParty.label}</span>
            <span className="text-stone-300 font-medium">{called.called_candidate || ''}</span>
            {called.leader_pct != null && (
              <span className="text-stone-400 text-xs tabular-nums">{Math.round(called.leader_pct)}%</span>
            )}
            <span className="text-[10px] text-stone-500 uppercase tracking-wide ml-1">
              {called.provider}{called.reporting_pct != null ? ` · ${Math.round(called.reporting_pct)}% in` : ''}
            </span>
          </div>
        ) : (
          <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-stone-800/50 border border-stone-800">
            <Activity className="h-4 w-4 text-stone-500 animate-pulse" aria-hidden="true" />
            <span className="text-sm text-stone-400">Uncalled</span>
          </div>
        )}
      </div>

      {/* Sources */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {sources.map(([src, data]) => {
          const top = data.top
          const p = pct(top?.probability)
          const partyColor = top?.inferred_party && PARTY_COLOR[top.inferred_party]
          // Highlight the source's bar if its inferred party disagrees with the call
          const isAgainstCall = called && partyColor && top?.inferred_party !== called.called_party && (top.probability || 0) >= 0.5
          return (
            <div key={src} className={`rounded-lg p-2.5 border ${isAgainstCall ? 'border-amber-500/60 bg-amber-500/5' : 'border-stone-800 bg-stone-800/40'}`}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] uppercase tracking-wide text-stone-500">{SOURCE_LABELS[src] || src}</span>
                {isAgainstCall && <AlertTriangle className="h-3 w-3 text-amber-400" aria-hidden="true" />}
              </div>
              {top && p != null ? (
                <>
                  <div className="flex items-baseline justify-between gap-1">
                    <span className={`text-[11px] truncate ${partyColor ? partyColor.text : 'text-stone-300'}`} title={top.name}>
                      {top.name}
                    </span>
                    <span className={`text-lg font-bold tabular-nums ${partyColor ? partyColor.text : 'text-stone-200'}`}>{p}%</span>
                  </div>
                  <div className="w-full bg-stone-800 rounded-full h-1 mt-1">
                    <div
                      className={`h-1 rounded-full ${partyColor ? partyColor.solid : 'bg-stone-500'}`}
                      style={{ width: `${Math.max(2, p)}%` }}
                    />
                  </div>
                </>
              ) : (
                <div className="text-xs text-stone-600 italic py-1">no data</div>
              )}
            </div>
          )
        })}
      </div>
    </article>
  )
}

export default function Live() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [paused, setPaused] = useState(false)
  const [showOnlyDisagreements, setShowOnlyDisagreements] = useState(false)
  const [lastUpdated, setLastUpdated] = useState(null)
  const timerRef = useRef(null)

  async function refresh() {
    try {
      const d = await api.liveDashboard()
      setData(d)
      setLastUpdated(new Date())
      setError(null)
    } catch (e) {
      setError(e.message || 'Live feed unavailable')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  useEffect(() => {
    if (paused) {
      if (timerRef.current) clearInterval(timerRef.current)
      return
    }
    timerRef.current = setInterval(refresh, 15000) // 15s
    return () => clearInterval(timerRef.current)
  }, [paused])

  const rows = useMemo(() => {
    const all = data?.rows || []
    return showOnlyDisagreements ? all.filter(r => r.disagreements?.length > 0) : all
  }, [data, showOnlyDisagreements])

  const providers = data?.providers || {}
  const noProviders = !providers.ap && !providers.ddhq

  return (
    <div className="-mx-4 sm:-mx-6 -my-6 sm:-my-8 min-h-screen bg-stone-950 text-stone-100">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6">
        <header className="flex items-center justify-between mb-4 gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <Tv className="h-6 w-6 text-emerald-400" aria-hidden="true" />
            <div>
              <h1 className="text-xl sm:text-2xl font-bold">Live election dashboard</h1>
              <p className="text-xs text-stone-500">
                {data ? (
                  <>
                    {data.totals.called}/{data.totals.races} called · {data.totals.disagreements} market disagreements
                    {lastUpdated && <span> · updated {timeAgo(lastUpdated.toISOString())}</span>}
                  </>
                ) : 'Loading…'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowOnlyDisagreements(v => !v)}
              aria-pressed={showOnlyDisagreements}
              className={`text-xs font-semibold px-3 py-1.5 rounded-md transition-colors ${
                showOnlyDisagreements ? 'bg-amber-500 text-stone-900' : 'bg-stone-800 text-stone-300 hover:bg-stone-700'
              }`}>
              <AlertTriangle className="h-3 w-3 inline mr-1" aria-hidden="true" />
              Disagreements only
            </button>
            <button onClick={() => setPaused(p => !p)}
              aria-pressed={paused}
              aria-label={paused ? 'Resume auto-refresh' : 'Pause auto-refresh'}
              className="text-xs font-semibold px-3 py-1.5 rounded-md bg-stone-800 text-stone-300 hover:bg-stone-700">
              {paused ? <Play className="h-3 w-3 inline mr-1" aria-hidden="true" /> : <Pause className="h-3 w-3 inline mr-1" aria-hidden="true" />}
              {paused ? 'Resume' : 'Pause'}
            </button>
            <button onClick={refresh} aria-label="Refresh now"
              className="text-xs font-semibold px-3 py-1.5 rounded-md bg-stone-800 text-stone-300 hover:bg-stone-700">
              <RefreshCw className="h-3 w-3 inline mr-1" aria-hidden="true" />
              Now
            </button>
          </div>
        </header>

        {noProviders && (
          <div role="status" className="mb-4 p-3 bg-stone-900 border border-stone-800 rounded-lg text-xs text-stone-400">
            <strong className="text-stone-300">Markets-only mode.</strong> No race-call provider configured. Set{' '}
            <code className="bg-stone-800 px-1 rounded">AP_API_KEY</code> or <code className="bg-stone-800 px-1 rounded">DDHQ_API_KEY</code>{' '}
            for live calls, then flip <code className="bg-stone-800 px-1 rounded">LIVE_NIGHT_MODE=1</code> on election night.
            Admins can manually call races for testing via <code className="bg-stone-800 px-1 rounded">POST /admin/race-call</code>.
          </div>
        )}

        {error && (
          <div role="alert" className="mb-4 p-3 bg-red-950/50 border border-red-800 rounded-lg text-sm text-red-200">
            {error}
          </div>
        )}

        {loading && !data ? (
          <div role="status" aria-live="polite" className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {[1,2,3,4,5,6].map(i => (
              <div key={i} className="bg-stone-900 border border-stone-800 rounded-xl h-44 animate-pulse" />
            ))}
            <span className="sr-only">Loading live dashboard…</span>
          </div>
        ) : rows.length === 0 ? (
          <div className="text-center py-12 text-stone-500 text-sm">
            {showOnlyDisagreements ? 'No disagreements right now. Markets and called races agree.' : 'No active races.'}
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {rows.map(row => <RaceCard key={row.race_key} row={row} />)}
          </div>
        )}
      </div>
    </div>
  )
}
