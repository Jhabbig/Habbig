import React, { useEffect, useState } from 'react'
import { api } from './api'
import { Target } from 'lucide-react'

// Module-level cache — accuracy stats are slow-moving, no point fetching the
// same (source, race_type) combo for every card on the page.
const _cache = new Map()

async function getCachedBadge(source, raceType) {
  const key = `${source}::${raceType || 'all'}`
  if (_cache.has(key)) return _cache.get(key)
  const promise = api.accuracyBadge(source, raceType).catch(() => null)
  _cache.set(key, promise)
  return promise
}

function colorFor(hitRate) {
  if (hitRate == null) return 'bg-stone-100 text-stone-500 border-stone-200'
  if (hitRate >= 0.85) return 'bg-emerald-100 text-emerald-700 border-emerald-200'
  if (hitRate >= 0.70) return 'bg-amber-100 text-amber-700 border-amber-200'
  return 'bg-red-100 text-red-700 border-red-200'
}

export default function AccuracyBadge({ source, raceType, compact = false, className = '' }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getCachedBadge(source, raceType).then((data) => {
      if (cancelled) return
      setStats(data)
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [source, raceType])

  if (loading) return null
  if (!stats || !stats.available || !stats.n) return null

  const pct = Math.round(stats.hit_rate * 100)
  const tooltip = [
    `${source}: ${pct}% hit rate`,
    raceType ? ` on ${raceType} races` : ` overall`,
    ` (n=${stats.n})`,
    `\nBrier ${stats.brier.toFixed(3)}`,
    stats.calibration_50 != null
      ? `\nToss-up calibration: ${Math.round(stats.calibration_50 * 100)}% (n=${stats.n_toss_ups})`
      : '',
  ].join('')

  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded border ${colorFor(stats.hit_rate)} ${className}`}
        title={tooltip}
        aria-label={tooltip}
      >
        <Target className="h-2.5 w-2.5" aria-hidden="true" />
        {pct}% (n={stats.n})
      </span>
    )
  }

  return (
    <div
      className={`inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-md border ${colorFor(stats.hit_rate)} ${className}`}
      title={tooltip}
    >
      <Target className="h-3 w-3" aria-hidden="true" />
      <span>{pct}% accurate</span>
      <span className="text-[10px] opacity-70">
        ({stats.n} {raceType ? `${raceType} ` : ''}race{stats.n === 1 ? '' : 's'})
      </span>
    </div>
  )
}
