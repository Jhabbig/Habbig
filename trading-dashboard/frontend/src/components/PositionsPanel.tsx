import React from 'react';
import { X } from 'lucide-react';
import type { Trade } from './OrderForm';

interface PositionsPanelProps {
  positions: Trade[];
  priceFor: (ticker: string) => number | null;
  onClosePosition: (id: string) => void;
}

const sideDirection = (side: Trade['side']) => (side === 'buy' ? 1 : -1);

export const PositionsPanel: React.FC<PositionsPanelProps> = ({ positions, priceFor, onClosePosition }) => {
  const openPositions = positions.filter((p) => !p.exitPrice);

  if (openPositions.length === 0) {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Open Positions</h3>
        <div className="text-center py-8 text-gray-400">
          <div className="text-5xl mb-2">📭</div>
          <p>No open positions</p>
        </div>
      </div>
    );
  }

  const totalQty = openPositions.reduce((sum, p) => sum + p.quantity, 0);
  const avgEntry = totalQty > 0
    ? openPositions.reduce((sum, p) => sum + p.entryPrice * p.quantity, 0) / totalQty
    : 0;

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
      <div className="p-4 border-b border-gray-700">
        <h3 className="text-lg font-semibold text-gray-100">Open Positions ({openPositions.length})</h3>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-700 bg-gray-900">
              <th className="text-left py-3 px-4 text-gray-400">Ticker</th>
              <th className="text-center py-3 px-4 text-gray-400">Side</th>
              <th className="text-right py-3 px-4 text-gray-400">Qty</th>
              <th className="text-right py-3 px-4 text-gray-400">Entry</th>
              <th className="text-right py-3 px-4 text-gray-400">Current</th>
              <th className="text-right py-3 px-4 text-gray-400">P&L</th>
              <th className="text-right py-3 px-4 text-gray-400">P&L %</th>
              <th className="text-center py-3 px-4 text-gray-400">Action</th>
            </tr>
          </thead>
          <tbody>
            {openPositions.map((position) => {
              const mark = priceFor(position.ticker);
              const dir = sideDirection(position.side);
              const hasMark = mark !== null;
              const pnl = hasMark ? (mark! - position.entryPrice) * position.quantity * dir : null;
              const pnlPct = hasMark ? ((mark! - position.entryPrice) / position.entryPrice) * 100 * dir : null;
              const isProfit = pnl !== null && pnl >= 0;

              return (
                <tr key={position.id} className="border-b border-gray-700 hover:bg-gray-700/50">
                  <td className="py-3 px-4 text-gray-100 font-semibold">{position.ticker}</td>
                  <td className="text-center py-3 px-4">
                    <span
                      className={`text-xs font-semibold px-2 py-1 rounded ${
                        position.side === 'buy'
                          ? 'bg-green-900/30 text-green-400'
                          : 'bg-red-900/30 text-red-400'
                      }`}
                    >
                      {position.side === 'buy' ? 'LONG' : 'SHORT'}
                    </span>
                  </td>
                  <td className="text-right py-3 px-4 text-gray-300">{position.quantity}</td>
                  <td className="text-right py-3 px-4 text-gray-300">${position.entryPrice.toFixed(2)}</td>
                  <td className="text-right py-3 px-4 text-gray-300">
                    {hasMark ? `$${mark!.toFixed(2)}` : <span className="text-gray-500 text-xs">no quote</span>}
                  </td>
                  <td className={`text-right py-3 px-4 font-semibold ${pnl === null ? 'text-gray-500' : isProfit ? 'text-green-400' : 'text-red-400'}`}>
                    {pnl === null ? '—' : `${isProfit ? '+' : ''}$${pnl.toFixed(2)}`}
                  </td>
                  <td className={`text-right py-3 px-4 font-semibold ${pnlPct === null ? 'text-gray-500' : isProfit ? 'text-green-400' : 'text-red-400'}`}>
                    {pnlPct === null ? '—' : `${isProfit ? '+' : ''}${pnlPct.toFixed(2)}%`}
                  </td>
                  <td className="text-center py-3 px-4">
                    <button
                      onClick={() => onClosePosition(position.id)}
                      disabled={!hasMark}
                      className="text-gray-400 hover:text-red-400 transition disabled:opacity-30 disabled:cursor-not-allowed"
                      title={hasMark ? 'Close position' : 'Waiting for quote'}
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Summary */}
      <div className="border-t border-gray-700 p-4 bg-gray-900/50">
        <div className="grid grid-cols-3 gap-4 text-sm">
          <div>
            <div className="text-gray-400 text-xs">Total Positions</div>
            <div className="text-gray-100 font-semibold">{openPositions.length}</div>
          </div>
          <div>
            <div className="text-gray-400 text-xs">Total Quantity</div>
            <div className="text-gray-100 font-semibold">{totalQty}</div>
          </div>
          <div>
            <div className="text-gray-400 text-xs">Avg Entry Price</div>
            <div className="text-gray-100 font-semibold">${avgEntry.toFixed(2)}</div>
          </div>
        </div>
      </div>
    </div>
  );
};
