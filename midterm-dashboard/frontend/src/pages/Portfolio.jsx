import React, { useState, useEffect, useMemo } from 'react'
import { api } from '../lib/api'
import { Briefcase, TrendingUp, TrendingDown, X } from 'lucide-react'

// Kelly criterion for a binary market: f* = (p*b - q) / b
// where p = your estimated probability, q = 1-p, b = decimal odds - 1.
// For a yes-side bet at price `entry`, payout is 1 for each $entry put in, so
// odds = 1/entry, b = (1-entry)/entry. We clamp to [0, 0.25] to avoid suggesting
// a Kelly stake bigger than 25% of bankroll.
export function kellyFraction(estimatedProb, entryPrice) {
  if (entryPrice <= 0 || entryPrice >= 1) return 0
  const p = Math.max(0, Math.min(1, estimatedProb))
  const q = 1 - p
  const b = (1 - entryPrice) / entryPrice
  if (b <= 0) return 0
  const f = (p * b - q) / b
  return Math.max(0, Math.min(0.25, f))
}

export function expectedValue(estimatedProb, entryPrice, side = 'yes') {
  if (side === 'yes') return estimatedProb * (1 - entryPrice) - (1 - estimatedProb) * entryPrice
  return (1 - estimatedProb) * entryPrice - estimatedProb * (1 - entryPrice)
}

function PositionPnL({ position }) {
  const isOpen = !position.closed_at
  // For open positions we don't have a current mark price here, so we just
  // show the entry. A future enhancement could fetch the current price.
  const pnl = isOpen ? 0 : (position.exit_price - position.entry_price) * (position.side === 'yes' ? 1 : -1) * position.size_usd
  if (isOpen) return <span className="text-stone-400 text-xs">open</span>
  return (
    <span className={`tabular-nums font-medium ${pnl >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
      {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
    </span>
  )
}

export default function Portfolio() {
  const [positions, setPositions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  // Kelly calculator state
  const [estProb, setEstProb] = useState(0.55)
  const [entryPrice, setEntryPrice] = useState(0.5)
  const [bankroll, setBankroll] = useState(1000)

  const kelly = useMemo(() => kellyFraction(estProb, entryPrice), [estProb, entryPrice])
  const stake = useMemo(() => bankroll * kelly, [bankroll, kelly])
  const ev = useMemo(() => expectedValue(estProb, entryPrice, 'yes'), [estProb, entryPrice])

  useEffect(() => {
    api.portfolio().then(d => setPositions(d.positions || []))
      .catch(e => setError(e.message || 'Failed to load portfolio'))
      .finally(() => setLoading(false))
  }, [])

  async function close(id) {
    const px = window.prompt('Exit price (0–1)?', '0.5')
    if (!px) return
    const n = Number(px)
    if (Number.isNaN(n) || n < 0 || n > 1) return
    try {
      await api.closePosition(id, n)
      const d = await api.portfolio()
      setPositions(d.positions || [])
    } catch (e) {
      setError(e.message || 'Failed to close position')
    }
  }

  const { realized, open } = useMemo(() => {
    let r = 0, o = 0
    for (const p of positions) {
      if (p.closed_at) r += (p.exit_price - p.entry_price) * (p.side === 'yes' ? 1 : -1) * p.size_usd
      else o += p.size_usd
    }
    return { realized: r, open: o }
  }, [positions])

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <div className="p-2 bg-emerald-50 rounded-lg"><Briefcase className="h-6 w-6 text-emerald-600" aria-hidden="true" /></div>
        <div>
          <h1 className="text-2xl font-bold text-stone-900">Paper portfolio</h1>
          <p className="text-stone-500 text-sm">Track hypothetical positions; not financial advice.</p>
        </div>
      </div>

      {error && <div role="alert" className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 mb-4 text-sm">{error}</div>}

      <div className="grid sm:grid-cols-3 gap-3 mb-6">
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4">
          <div className="text-[11px] text-stone-400 uppercase tracking-wide">Realized P&amp;L</div>
          <div className={`text-2xl font-bold tabular-nums ${realized >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
            {realized >= 0 ? '+' : ''}${realized.toFixed(2)}
          </div>
        </div>
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4">
          <div className="text-[11px] text-stone-400 uppercase tracking-wide">Open exposure</div>
          <div className="text-2xl font-bold tabular-nums text-stone-800">${open.toFixed(2)}</div>
        </div>
        <div className="bg-white shadow-sm border border-stone-100 rounded-xl p-4">
          <div className="text-[11px] text-stone-400 uppercase tracking-wide">Open positions</div>
          <div className="text-2xl font-bold tabular-nums text-stone-800">{positions.filter(p => !p.closed_at).length}</div>
        </div>
      </div>

      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5 mb-6">
        <h2 className="text-sm font-semibold text-stone-800 mb-3 flex items-center gap-1.5">
          <TrendingUp className="h-4 w-4 text-stone-500" aria-hidden="true" />Kelly + EV calculator
        </h2>
        <div className="grid sm:grid-cols-3 gap-3 text-sm">
          <label className="block">
            <span className="text-xs text-stone-500 block mb-1">Your est. probability</span>
            <input type="number" step="0.01" min="0" max="1" value={estProb}
              onChange={e => setEstProb(Number(e.target.value))}
              aria-label="Your estimated probability between 0 and 1"
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-2 py-1 tabular-nums" />
          </label>
          <label className="block">
            <span className="text-xs text-stone-500 block mb-1">Market price (yes)</span>
            <input type="number" step="0.01" min="0" max="1" value={entryPrice}
              onChange={e => setEntryPrice(Number(e.target.value))}
              aria-label="Market price between 0 and 1"
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-2 py-1 tabular-nums" />
          </label>
          <label className="block">
            <span className="text-xs text-stone-500 block mb-1">Bankroll ($)</span>
            <input type="number" step="10" min="0" value={bankroll}
              onChange={e => setBankroll(Number(e.target.value))}
              aria-label="Total bankroll in dollars"
              className="w-full bg-stone-50 border border-stone-200 rounded-lg px-2 py-1 tabular-nums" />
          </label>
        </div>
        <div className="grid sm:grid-cols-3 gap-3 mt-4 text-sm">
          <div className="bg-stone-50 border border-stone-100 rounded-lg p-3">
            <div className="text-[11px] text-stone-400 uppercase tracking-wide">Kelly fraction</div>
            <div className="text-lg font-bold tabular-nums text-stone-800">{(kelly * 100).toFixed(1)}%</div>
          </div>
          <div className="bg-stone-50 border border-stone-100 rounded-lg p-3">
            <div className="text-[11px] text-stone-400 uppercase tracking-wide">Suggested stake</div>
            <div className="text-lg font-bold tabular-nums text-stone-800">${stake.toFixed(2)}</div>
          </div>
          <div className="bg-stone-50 border border-stone-100 rounded-lg p-3">
            <div className="text-[11px] text-stone-400 uppercase tracking-wide">Expected value / $1</div>
            <div className={`text-lg font-bold tabular-nums ${ev >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
              {ev >= 0 ? '+' : ''}${ev.toFixed(3)}
            </div>
          </div>
        </div>
        <p className="text-[11px] text-stone-400 mt-3">Kelly is capped at 25% to avoid blow-up; many traders use half-Kelly. Negative EV → don't bet.</p>
      </section>

      <section className="bg-white shadow-sm border border-stone-100 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-stone-800 mb-3">Positions</h2>
        {loading ? (
          <div role="status" aria-label="Loading positions" className="text-stone-400 text-sm">Loading…</div>
        ) : positions.length === 0 ? (
          <div className="text-sm text-stone-400">No positions yet. Open one from a race detail page.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-[11px] text-stone-400 uppercase tracking-wide">
                <tr>
                  <th scope="col" className="text-left py-2">Race</th>
                  <th scope="col" className="text-left py-2">Source</th>
                  <th scope="col" className="text-left py-2">Side</th>
                  <th scope="col" className="text-right py-2">Entry</th>
                  <th scope="col" className="text-right py-2">Size</th>
                  <th scope="col" className="text-right py-2">Exit</th>
                  <th scope="col" className="text-right py-2">P&amp;L</th>
                  <th scope="col" className="py-2"></th>
                </tr>
              </thead>
              <tbody>
                {positions.map(p => (
                  <tr key={p.id} className="border-t border-stone-50">
                    <td className="py-2">{p.race_key}</td>
                    <td className="py-2 capitalize">{p.source}</td>
                    <td className="py-2 uppercase text-xs">
                      {p.side === 'yes'
                        ? <span className="text-emerald-600 inline-flex items-center gap-0.5"><TrendingUp className="h-3 w-3" aria-hidden="true" />yes</span>
                        : <span className="text-red-600 inline-flex items-center gap-0.5"><TrendingDown className="h-3 w-3" aria-hidden="true" />no</span>}
                    </td>
                    <td className="py-2 text-right tabular-nums">{(p.entry_price * 100).toFixed(0)}%</td>
                    <td className="py-2 text-right tabular-nums">${p.size_usd.toFixed(0)}</td>
                    <td className="py-2 text-right tabular-nums">{p.exit_price ? `${(p.exit_price * 100).toFixed(0)}%` : '—'}</td>
                    <td className="py-2 text-right"><PositionPnL position={p} /></td>
                    <td className="py-2 text-right">
                      {!p.closed_at && (
                        <button onClick={() => close(p.id)} aria-label="Close position"
                          className="text-stone-400 hover:text-stone-700">
                          <X className="h-4 w-4" aria-hidden="true" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
