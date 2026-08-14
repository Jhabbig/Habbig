import React from 'react';
import { TrendingUp, TrendingDown, DollarSign, BarChart3 } from 'lucide-react';

export interface BacktestResult {
  ticker: string;
  strategy: string;
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_equity: number;
  total_return_pct: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  profit_factor: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  calmar_ratio: number;
  max_drawdown_pct: number;
  avg_drawdown_pct: number;
  equity_curve: Array<{ time: number; value: number }>;
  trades: Array<any>;
  bar_count: number;
}

interface BacktestResultsProps {
  result: BacktestResult;
}

export const BacktestResults: React.FC<BacktestResultsProps> = ({ result }) => {
  const isPositive = result.total_return_pct >= 0;
  const startDate = new Date(result.start_date).toLocaleDateString();
  const endDate = new Date(result.end_date).toLocaleDateString();

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
        <h3 className="text-lg font-semibold text-gray-100 mb-4">Performance Summary</h3>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {/* Total Return */}
          <div className="bg-gray-900 p-4 rounded border border-gray-700">
            <div className="text-gray-400 text-sm mb-1">Total Return</div>
            <div className={`text-2xl font-bold flex items-center gap-2 ${isPositive ? 'text-green-400' : 'text-red-400'}`}>
              {isPositive ? <TrendingUp className="w-5 h-5" /> : <TrendingDown className="w-5 h-5" />}
              {result.total_return_pct.toFixed(2)}%
            </div>
            <div className="text-xs text-gray-500 mt-2">
              ${result.initial_capital.toLocaleString()} → ${result.final_equity.toLocaleString()}
            </div>
          </div>

          {/* Sharpe Ratio */}
          <div className="bg-gray-900 p-4 rounded border border-gray-700">
            <div className="text-gray-400 text-sm mb-1">Sharpe Ratio</div>
            <div className="text-2xl font-bold text-blue-400">
              {result.sharpe_ratio.toFixed(2)}
            </div>
            <div className="text-xs text-gray-500 mt-2">Risk-adjusted return</div>
          </div>

          {/* Max Drawdown */}
          <div className="bg-gray-900 p-4 rounded border border-gray-700">
            <div className="text-gray-400 text-sm mb-1">Max Drawdown</div>
            <div className="text-2xl font-bold text-orange-400">
              {result.max_drawdown_pct.toFixed(2)}%
            </div>
            <div className="text-xs text-gray-500 mt-2">Peak to trough</div>
          </div>

          {/* Win Rate */}
          <div className="bg-gray-900 p-4 rounded border border-gray-700">
            <div className="text-gray-400 text-sm mb-1">Win Rate</div>
            <div className="text-2xl font-bold text-purple-400">
              {result.win_rate.toFixed(1)}%
            </div>
            <div className="text-xs text-gray-500 mt-2">
              {result.winning_trades}W / {result.losing_trades}L
            </div>
          </div>
        </div>
      </div>

      {/* Detailed Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Trade Statistics */}
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <h4 className="text-md font-semibold text-gray-100 mb-3">Trade Statistics</h4>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-400">Total Trades</span>
              <span className="text-gray-100 font-medium">{result.total_trades}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-400">Winning Trades</span>
              <span className="text-green-400 font-medium">{result.winning_trades}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-400">Losing Trades</span>
              <span className="text-red-400 font-medium">{result.losing_trades}</span>
            </div>
            <div className="border-t border-gray-700 pt-2 mt-2">
              <div className="flex justify-between">
                <span className="text-gray-400">Avg Winner</span>
                <span className="text-green-400 font-medium">${result.avg_win.toFixed(2)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Avg Loser</span>
                <span className="text-red-400 font-medium">-${Math.abs(result.avg_loss).toFixed(2)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Profit Factor</span>
                <span className="text-blue-400 font-medium">{result.profit_factor.toFixed(2)}</span>
              </div>
            </div>
          </div>
        </div>

        {/* Risk Metrics */}
        <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
          <h4 className="text-md font-semibold text-gray-100 mb-3">Risk Metrics</h4>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-400">Sharpe Ratio</span>
              <span className="text-blue-400 font-medium">{result.sharpe_ratio.toFixed(2)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-400">Sortino Ratio</span>
              <span className="text-blue-400 font-medium">{result.sortino_ratio.toFixed(2)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-400">Calmar Ratio</span>
              <span className="text-blue-400 font-medium">{result.calmar_ratio.toFixed(2)}</span>
            </div>
            <div className="border-t border-gray-700 pt-2 mt-2">
              <div className="flex justify-between">
                <span className="text-gray-400">Max Drawdown</span>
                <span className="text-orange-400 font-medium">{result.max_drawdown_pct.toFixed(2)}%</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-400">Avg Drawdown</span>
                <span className="text-orange-400 font-medium">{result.avg_drawdown_pct.toFixed(2)}%</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Date Range */}
      <div className="bg-gray-800 border border-gray-700 rounded-lg p-4 text-sm text-gray-400">
        <div className="flex justify-between items-center">
          <div>
            <span className="font-medium">Backtest Period:</span> {startDate} to {endDate}
          </div>
          <div>
            <span className="font-medium">Bars Analyzed:</span> {result.bar_count}
          </div>
        </div>
      </div>

      {/* Recent Trades */}
      {result.trades.length > 0 && (
        <div className="bg-gray-800 border border-gray-700 rounded-lg overflow-hidden">
          <div className="p-4 border-b border-gray-700">
            <h4 className="text-md font-semibold text-gray-100 flex items-center gap-2">
              <BarChart3 className="w-5 h-5" />
              Recent Trades (Last 10)
            </h4>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-gray-700 bg-gray-900">
                  <th className="text-left py-2 px-3 text-gray-400">Entry</th>
                  <th className="text-right py-2 px-3 text-gray-400">Entry Price</th>
                  <th className="text-right py-2 px-3 text-gray-400">Exit Price</th>
                  <th className="text-right py-2 px-3 text-gray-400">P&L</th>
                  <th className="text-right py-2 px-3 text-gray-400">P&L %</th>
                  <th className="text-left py-2 px-3 text-gray-400">Reason</th>
                </tr>
              </thead>
              <tbody>
                {result.trades.slice(-10).reverse().map((trade, idx) => (
                  <tr key={idx} className="border-b border-gray-700 hover:bg-gray-700">
                    <td className="py-2 px-3 text-gray-300">
                      {new Date(trade.entry_time * 1000).toLocaleDateString()}
                    </td>
                    <td className="text-right py-2 px-3 text-gray-300">
                      ${trade.entry_price.toFixed(2)}
                    </td>
                    <td className="text-right py-2 px-3 text-gray-300">
                      ${trade.exit_price.toFixed(2)}
                    </td>
                    <td className={`text-right py-2 px-3 font-medium ${trade.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      ${trade.pnl.toFixed(2)}
                    </td>
                    <td className={`text-right py-2 px-3 font-medium ${trade.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {trade.pnl_pct.toFixed(2)}%
                    </td>
                    <td className="py-2 px-3 text-gray-400">{trade.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
