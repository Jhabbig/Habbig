import React from 'react';
import { TrendingUp, TrendingDown, X } from 'lucide-react';
import type { Trade } from './OrderForm';

interface PositionsPanelProps {
  positions: Trade[];
  currentPrice: number;
  onClosePosition: (id: string) => void;
}

export const PositionsPanel: React.FC<PositionsPanelProps> = ({ positions, currentPrice, onClosePosition }) => {
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
              const pnl = (currentPrice - position.entryPrice) * position.quantity;
              const pnlPct = ((currentPrice - position.entryPrice) / position.entryPrice) * 100;
              const isProfit = pnl >= 0;

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
                  <td className="text-right py-3 px-4 text-gray-300">${currentPrice.toFixed(2)}</td>
                  <td className={`text-right py-3 px-4 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                    {isProfit ? '+' : ''} ${pnl.toFixed(2)}
                  </td>
                  <td className={`text-right py-3 px-4 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                    {isProfit ? '+' : ''}
                    {pnlPct.toFixed(2)}%
                  </td>
                  <td className="text-center py-3 px-4">
                    <button
                      onClick={() => onClosePosition(position.id)}
                      className="text-gray-400 hover:text-red-400 transition"
                      title="Close position"
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
            <div className="text-gray-100 font-semibold">
              {openPositions.reduce((sum, p) => sum + p.quantity, 0)}
            </div>
          </div>
          <div>
            <div className="text-gray-400 text-xs">Avg Entry Price</div>
            <div className="text-gray-100 font-semibold">
              $
              {(
                openPositions.reduce((sum, p) => sum + p.entryPrice * p.quantity, 0) /
                openPositions.reduce((sum, p) => sum + p.quantity, 0)
              ).toFixed(2)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
