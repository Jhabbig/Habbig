import React from 'react'

// Single source of truth for source colours / labels and party colours.
// RaceDetail used to inline these copies; the Backtest page (and any future
// per-source UI) reuses them so adding a source updates the whole UI in one
// place.
export const sourceColors = {
  polymarket: '#8b5cf6',
  kalshi: '#3b82f6',
  predictit: '#f59e0b',
  polling: '#10b981',
  metaculus: '#a855f7',
  manifold: '#ec4899',
}

export const sourceLabels = {
  polymarket: 'Polymarket',
  kalshi: 'Kalshi',
  predictit: 'PredictIt',
  polling: '538 / RCP Polling',
  metaculus: 'Metaculus',
  manifold: 'Manifold',
}

export const PARTY_COLORS = { DEM: '#3b82f6', REP: '#ef4444', IND: '#f59e0b' }

export function partyColor(party) {
  if (!party) return '#78716c'
  const p = party.toUpperCase()
  if (p.startsWith('DEM') || p === 'D') return PARTY_COLORS.DEM
  if (p.startsWith('REP') || p === 'R') return PARTY_COLORS.REP
  return '#78716c'
}

// A single horizontal probability bar. Used for outcome lists in races
// and source comparisons in the backtest page.
export function OutcomeBar({ name, probability, maxProb, color }) {
  const pct = (probability || 0) * 100
  const width = maxProb > 0 ? (probability / maxProb) * 100 : 0
  return (
    <div className="flex items-center gap-3 py-1">
      <span className="text-xs text-stone-600 w-28 truncate flex-shrink-0" title={name}>{name}</span>
      <div className="flex-1 h-5 bg-stone-100 rounded overflow-hidden relative">
        <div
          className="h-full rounded transition-all"
          style={{ width: `${width}%`, backgroundColor: color || '#78716c' }}
        />
      </div>
      <span className="text-xs font-bold text-stone-800 tabular-nums w-12 text-right">
        {pct.toFixed(1)}%
      </span>
    </div>
  )
}
