import React, { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { sourceColors, sourceLabels } from '../lib/raceTheme.jsx'
import { Newspaper, ArrowUpRight, Clock, Zap } from 'lucide-react'

function fmtAge(iso) {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(ms) || ms < 0) return ''
  const m = Math.round(ms / 60000)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h}h ago`
  const d = Math.round(h / 24)
  return `${d}d ago`
}

function fmtLag(seconds) {
  if (seconds == null) return null
  if (seconds < 60) return `${seconds}s`
  const m = Math.round(seconds / 60)
  if (m < 60) return `${m}m`
  const h = Math.round(seconds / 360) / 10
  return `${h}h`
}

// Per-race news timeline with measured market reaction strips. When a news
// item has reactions recorded, we render one chip per source showing the
// price move and the lag-from-publish. That's the moment-of-truth view: did
// the market actually move after this headline, and how fast?
export default function NewsTimeline({ raceKey }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!raceKey) return
    setLoading(true)
    api.newsForRace(raceKey)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [raceKey])

  if (loading) {
    return (
      <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-3">
          <Newspaper className="h-4 w-4 text-stone-500" />
          <h3 className="text-sm font-semibold text-stone-800">News + market reaction</h3>
        </div>
        <div className="space-y-2">
          {[1, 2, 3].map((i) => <div key={i} className="h-10 bg-stone-100 rounded animate-pulse" />)}
        </div>
      </div>
    )
  }
  const items = data?.items || []
  if (!items.length) return null

  return (
    <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-stone-800 flex items-center gap-2">
          <Newspaper className="h-4 w-4 text-stone-500" />
          News + market reaction
          <span className="text-xs font-normal text-stone-400">{items.length}</span>
        </h3>
      </div>
      <ul className="divide-y divide-stone-100">
        {items.map((n) => {
          const reactions = (n.reactions || []).filter((r) => (r.delta_pp || 0) >= 1.0)
          return (
            <li key={n.id} className="py-3">
              <a
                href={n.link || '#'}
                target="_blank"
                rel="noopener noreferrer"
                className="block group"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-stone-800 group-hover:text-stone-900">
                      {n.title}
                      {n.link && (
                        <ArrowUpRight className="inline h-3 w-3 ml-1 text-stone-400 group-hover:text-stone-600" />
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-1 text-[11px] text-stone-400">
                      <span>{n.source}</span>
                      <span>·</span>
                      <Clock className="h-3 w-3" />
                      <span>{fmtAge(n.published_at)}</span>
                    </div>
                  </div>
                </div>
              </a>
              {reactions.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {reactions.map((r, i) => {
                    const dir = (r.reaction_price ?? r.baseline_price) > r.baseline_price ? '+' : '−'
                    const c = sourceColors[r.source] || '#78716c'
                    const lag = fmtLag(r.lag_seconds)
                    return (
                      <span
                        key={i}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border"
                        style={{ borderColor: `${c}55`, color: c, background: `${c}11` }}
                        title={`${sourceLabels[r.source] || r.source}: ${r.baseline_price?.toFixed(2)} → ${r.reaction_price?.toFixed(2)}${lag ? ` in ${lag}` : ''}`}
                      >
                        <Zap className="h-3 w-3" />
                        {sourceLabels[r.source] || r.source}: {dir}{Math.abs(r.delta_pp).toFixed(1)}pp
                        {lag && <span className="text-stone-400">· {lag}</span>}
                      </span>
                    )
                  })}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
