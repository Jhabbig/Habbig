import React from 'react';
import { TrendingUp, TrendingDown, BookOpen } from 'lucide-react';
import type { Trade } from './OrderForm';

interface TradeJournalProps {
  trades: Trade[];
}

export const TradeJournal: React.FC<TradeJournalProps> = ({ trades }) => {
  const closedTrades = trades.filter((t) => t.exitPrice).sort((a, b) => (b.exitTime || 0) - (a.exitTime || 0));

  if (closedTrades.length === 0) {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
        <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
          <BookOpen className="w-5 h-5" />
          Trade Journal
        </h3>
        <div className="text-center py-8 text-gray-400">
          <div className="text-5xl mb-2">📔</div>
          <p>No completed trades yet</p>
        </div>
      </div>
    );
  }

  // Calculate summary stats
  const winningTrades = closedTrades.filter((t) => (t.pnl || 0) > 0);
  const losingTrades = closedTrades.filter((t) => (t.pnl || 0) < 0);
  const totalPnl = closedTrades.reduce((sum, t) => sum + (t.pnl || 0), 0);
  const winRate = (winningTrades.length / closedTrades.length) * 100;

  return (
    <div className="space-y-4">
      {/* Summary Stats */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Total Trades</div>
          <div className="text-2xl font-bold text-gray-100">{closedTrades.length}</div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Win Rate</div>
          <div className="text-2xl font-bold text-gray-100">{winRate.toFixed(1)}%</div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Avg Win</div>
          <div className={`text-2xl font-bold ${winningTrades.length > 0 ? 'text-green-400' : 'text-gray-400'}`}>
            ${winningTrades.length > 0 ? (winningTrades.reduce((sum, t) => sum + (t.pnl || 0), 0) / winningTrades.length).toFixed(2) : '0.00'}
          </div>
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <div className="text-sm text-gray-400 mb-1">Total P&L</div>
          <div className={`text-2xl font-bold ${totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}
          </div>
        </div>
      </div>

      {/* Trades Table */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
        <div className="p-4 border-b border-gray-700">
          <h3 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
            <BookOpen className="w-5 h-5" />
            Recent Trades (Last 20)
          </h3>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-700 bg-gray-900">
                <th className="text-left py-3 px-4 text-gray-400">Ticker</th>
                <th className="text-center py-3 px-4 text-gray-400">Side</th>
                <th className="text-right py-3 px-4 text-gray-400">Qty</th>
                <th className="text-right py-3 px-4 text-gray-400">Entry</th>
                <th className="text-right py-3 px-4 text-gray-400">Exit</th>
                <th className="text-right py-3 px-4 text-gray-400">P&L</th>
                <th className="text-right py-3 px-4 text-gray-400">P&L %</th>
                <th className="text-left py-3 px-4 text-gray-400">Duration</th>
                <th className="text-left py-3 px-4 text-gray-400">Reason</th>
              </tr>
            </thead>
            <tbody>
              {closedTrades.slice(-20).reverse().map((trade) => {
                const duration = Math.floor(((trade.exitTime || 0) - trade.entryTime) / 60);
                const isProfit = (trade.pnl || 0) >= 0;

                return (
                  <tr key={trade.id} className="border-b border-gray-700 hover:bg-gray-700/50">
                    <td className="py-3 px-4 text-gray-100 font-semibold">{trade.ticker}</td>
                    <td className="text-center py-3 px-4">
                      <span
                        className={`text-xs font-semibold px-2 py-1 rounded ${
                          trade.side === 'buy'
                            ? 'bg-green-900/30 text-green-400'
                            : 'bg-red-900/30 text-red-400'
                        }`}
                      >
                        {trade.side === 'buy' ? 'BUY' : 'SELL'}
                      </span>
                    </td>
                    <td className="text-right py-3 px-4 text-gray-300">{trade.quantity}</td>
                    <td className="text-right py-3 px-4 text-gray-300">${trade.entryPrice.toFixed(2)}</td>
                    <td className="text-right py-3 px-4 text-gray-300">${(trade.exitPrice || 0).toFixed(2)}</td>
                    <td className={`text-right py-3 px-4 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                      {isProfit ? '+' : ''}${(trade.pnl || 0).toFixed(2)}
                    </td>
                    <td className={`text-right py-3 px-4 font-semibold ${isProfit ? 'text-green-400' : 'text-red-400'}`}>
                      {isProfit ? '+' : ''}
                      {(trade.pnlPct || 0).toFixed(2)}%
                    </td>
                    <td className="py-3 px-4 text-gray-400 text-xs">
                      {duration > 60 ? `${Math.floor(duration / 60)}h` : `${duration}m`}
                    </td>
                    <td className="py-3 px-4 text-gray-400 text-xs">{trade.reason || '-'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
