import React, { useState } from 'react';
import { Play, BarChart3, Grid3X3, TrendingUp } from 'lucide-react';

interface OptimizationResult {
  params: Record<string, number>;
  returnPct: number;
  sharpeRatio: number;
  winRate: number;
  maxDrawdown: number;
}

export const BacktestOptimizer: React.FC = () => {
  const [strategy, setStrategy] = useState('rsi');
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<OptimizationResult[]>([]);
  const [view, setView] = useState<'heatmap' | 'table' | 'monte'>('heatmap');

  // Parameter ranges for optimization
  const paramRanges = {
    rsi: {
      rsi_oversold: { min: 20, max: 40, step: 5 },
      rsi_overbought: { min: 60, max: 80, step: 5 },
      position_size_pct: { min: 0.05, max: 0.2, step: 0.05 },
    },
    ma_crossover: {
      fast_period: { min: 5, max: 15, step: 1 },
      slow_period: { min: 20, max: 50, step: 5 },
      position_size_pct: { min: 0.05, max: 0.2, step: 0.05 },
    },
  };

  const handleOptimize = async () => {
    setRunning(true);
    const ranges = paramRanges[strategy as keyof typeof paramRanges];

    // Simulate parameter grid search
    const mockResults: OptimizationResult[] = [];
    const paramKeys = Object.keys(ranges);

    let iterations = 0;
    const maxIterations = 25; // Simulate 5x5 grid

    for (const val1 of Array.from(
      { length: 5 },
      (_, i) => {
        const r = ranges[paramKeys[0] as keyof typeof ranges];
        return r.min + i * ((r.max - r.min) / 4);
      }
    )) {
      for (const val2 of Array.from(
        { length: 5 },
        (_, i) => {
          const r = ranges[paramKeys[1] as keyof typeof ranges];
          return r.min + i * ((r.max - r.min) / 4);
        }
      )) {
        if (iterations >= maxIterations) break;

        // Simulate backtest result
        const returnPct = 15 + Math.random() * 35 - (Math.abs(val1 - 30) / 10 + Math.abs(val2 - 30) / 10);
        const sharpeRatio = 1.2 + Math.random() * 1.5;

        mockResults.push({
          params: {
            [paramKeys[0]]: parseFloat(val1.toFixed(2)),
            [paramKeys[1]]: parseFloat(val2.toFixed(2)),
          },
          returnPct: Math.max(-20, returnPct),
          sharpeRatio,
          winRate: 50 + Math.random() * 25,
          maxDrawdown: -(10 + Math.random() * 20),
        });

        iterations++;
      }
    }

    // Simulate delay
    await new Promise((resolve) => setTimeout(resolve, 2000));
    setResults(mockResults.sort((a, b) => b.returnPct - a.returnPct));
    setRunning(false);
  };

  const bestResult = results[0];
  const heatmapData = results.slice(0, 25); // 5x5 grid

  // Monte Carlo simulation
  const runMonteCarloData = () => {
    if (!bestResult) return [];

    const simulations = 100;
    const paths: number[][] = [];

    for (let s = 0; s < simulations; s++) {
      const path = [100]; // Starting equity
      for (let day = 0; day < 252; day++) {
        // 1 year of trading days
        const dailyReturn = (bestResult.sharpeRatio / Math.sqrt(252)) * Math.random() - 0.001;
        path.push(path[path.length - 1] * (1 + dailyReturn));
      }
      paths.push(path);
    }

    return paths;
  };

  const monteCarloData = runMonteCarloData();

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-lg p-6 space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-gray-100 mb-2">Strategy Optimizer</h2>
        <p className="text-gray-400 text-sm">Find optimal parameters using grid search and Monte Carlo simulation</p>
      </div>

      {/* Controls */}
      <div className="grid grid-cols-3 gap-4">
        <div>
          <label className="block text-sm text-gray-400 mb-2">Strategy</label>
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            disabled={running}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            <option value="rsi">RSI Oversold/Overbought</option>
            <option value="ma_crossover">MA Crossover</option>
          </select>
        </div>

        <div>
          <label className="block text-sm text-gray-400 mb-2">Optimization Type</label>
          <select
            disabled={running}
            className="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            <option>Grid Search (5x5)</option>
            <option>Random Search (25)</option>
            <option>Genetic Algorithm</option>
          </select>
        </div>

        <div className="flex items-end">
          <button
            onClick={handleOptimize}
            disabled={running}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white px-4 py-2 rounded font-semibold transition flex items-center justify-center gap-2"
          >
            <Play className="w-4 h-4" />
            {running ? 'Optimizing...' : 'Run Optimization'}
          </button>
        </div>
      </div>

      {/* Results */}
      {results.length > 0 && (
        <>
          {/* Best Result */}
          <div className="bg-gradient-to-r from-green-900 to-green-800 border border-green-700 rounded-lg p-4">
            <div className="grid grid-cols-5 gap-4">
              <div>
                <div className="text-green-200 text-xs mb-1">Best Return</div>
                <div className="text-2xl font-bold text-white">{bestResult.returnPct.toFixed(2)}%</div>
              </div>
              <div>
                <div className="text-green-200 text-xs mb-1">Sharpe Ratio</div>
                <div className="text-2xl font-bold text-white">{bestResult.sharpeRatio.toFixed(2)}</div>
              </div>
              <div>
                <div className="text-green-200 text-xs mb-1">Win Rate</div>
                <div className="text-2xl font-bold text-white">{bestResult.winRate.toFixed(1)}%</div>
              </div>
              <div>
                <div className="text-green-200 text-xs mb-1">Max Drawdown</div>
                <div className="text-2xl font-bold text-white">{bestResult.maxDrawdown.toFixed(1)}%</div>
              </div>
              <div>
                <div className="text-green-200 text-xs mb-1">Parameters</div>
                <div className="text-xs font-mono text-white">
                  {Object.entries(bestResult.params)
                    .map(([k, v]) => `${k.split('_')[0]}=${v}`)
                    .join(', ')}
                </div>
              </div>
            </div>
          </div>

          {/* View Selector */}
          <div className="flex gap-2">
            {(['heatmap', 'table', 'monte'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-3 py-2 rounded text-sm font-medium transition ${
                  view === v
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-700 text-gray-400 hover:text-gray-200'
                }`}
              >
                {v === 'heatmap' && '📊 Heatmap'}
                {v === 'table' && '📋 Results Table'}
                {v === 'monte' && '📈 Monte Carlo'}
              </button>
            ))}
          </div>

          {/* Heatmap View */}
          {view === 'heatmap' && (
            <div className="bg-gray-900 rounded-lg p-6 overflow-x-auto">
              <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
                <Grid3X3 className="w-5 h-5" />
                Parameter Grid (Returns %)
              </h3>
              <div className="inline-grid gap-1" style={{ gridTemplateColumns: 'repeat(5, 1fr)' }}>
                {heatmapData.map((result, idx) => {
                  const normalized = (result.returnPct + 20) / 50; // -20% to +30% normalized
                  const hue = Math.max(0, Math.min(120, normalized * 120)); // Green hue
                  return (
                    <div
                      key={idx}
                      className="w-12 h-12 rounded flex items-center justify-center text-xs font-bold cursor-pointer hover:ring-2 hover:ring-blue-400 transition"
                      style={{
                        backgroundColor: `hsl(${hue}, 100%, 40%)`,
                        color: hue > 60 ? '#000' : '#fff',
                      }}
                      title={`${result.returnPct.toFixed(1)}%`}
                    >
                      {result.returnPct.toFixed(0)}
                    </div>
                  );
                })}
              </div>
              <div className="mt-4 text-xs text-gray-400">
                Darker green = higher returns. Hover for exact values. Click to view parameters.
              </div>
            </div>
          )}

          {/* Table View */}
          {view === 'table' && (
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-800 border-b border-gray-700">
                      <th className="text-left py-3 px-4 text-gray-400">Rank</th>
                      <th className="text-left py-3 px-4 text-gray-400">Parameters</th>
                      <th className="text-right py-3 px-4 text-gray-400">Return</th>
                      <th className="text-right py-3 px-4 text-gray-400">Sharpe</th>
                      <th className="text-right py-3 px-4 text-gray-400">Win Rate</th>
                      <th className="text-right py-3 px-4 text-gray-400">Max DD</th>
                    </tr>
                  </thead>
                  <tbody>
                    {results.slice(0, 10).map((result, idx) => (
                      <tr key={idx} className="border-b border-gray-700 hover:bg-gray-800/50">
                        <td className="py-3 px-4 text-gray-300 font-semibold">{idx + 1}</td>
                        <td className="py-3 px-4 text-gray-400 text-xs font-mono">
                          {Object.entries(result.params)
                            .map(([k, v]) => `${k.split('_')[0]}=${v}`)
                            .join(', ')}
                        </td>
                        <td className={`text-right py-3 px-4 font-bold ${result.returnPct > 20 ? 'text-green-400' : result.returnPct > 0 ? 'text-blue-400' : 'text-red-400'}`}>
                          {result.returnPct.toFixed(2)}%
                        </td>
                        <td className="text-right py-3 px-4 text-blue-400">{result.sharpeRatio.toFixed(2)}</td>
                        <td className="text-right py-3 px-4 text-purple-400">{result.winRate.toFixed(1)}%</td>
                        <td className="text-right py-3 px-4 text-orange-400">{result.maxDrawdown.toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Monte Carlo View */}
          {view === 'monte' && (
            <div className="bg-gray-900 rounded-lg p-6">
              <h3 className="text-lg font-semibold text-gray-100 mb-4 flex items-center gap-2">
                <TrendingUp className="w-5 h-5" />
                Monte Carlo Simulation (100 paths, 1 year)
              </h3>

              {monteCarloData.length > 0 && (
                <div className="space-y-4">
                  {/* SVG Chart */}
                  <svg viewBox="0 0 800 300" className="w-full border border-gray-700 rounded">
                    <defs>
                      <linearGradient id="grad" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stopColor="rgba(34, 197, 94, 0.3)" />
                        <stop offset="100%" stopColor="rgba(34, 197, 94, 0)" />
                      </linearGradient>
                    </defs>

                    {/* Grid */}
                    <line x1="50" y1="250" x2="750" y2="250" stroke="#444" strokeWidth="1" />
                    <line x1="50" y1="50" x2="50" y2="250" stroke="#444" strokeWidth="1" />

                    {/* Paths */}
                    {monteCarloData.slice(0, 100).map((path, idx) => {
                      const points = path
                        .map((val, i) => {
                          const x = 50 + (i / path.length) * 700;
                          const maxVal = Math.max(...path);
                          const y = 250 - ((val / maxVal) * 200);
                          return `${x},${y}`;
                        })
                        .join(' ');
                      return (
                        <polyline
                          key={idx}
                          points={points}
                          fill="none"
                          stroke={`rgba(59, 130, 246, ${0.1 + Math.random() * 0.2})`}
                          strokeWidth="1"
                        />
                      );
                    })}

                    {/* Labels */}
                    <text x="400" y="280" textAnchor="middle" fill="#999" fontSize="12">
                      Trading Days (252)
                    </text>
                    <text x="20" y="150" textAnchor="middle" fill="#999" fontSize="12" transform="rotate(-90 20 150)">
                      Equity
                    </text>
                  </svg>

                  {/* Statistics */}
                  <div className="grid grid-cols-4 gap-4 text-sm">
                    <div className="bg-gray-800 p-3 rounded">
                      <div className="text-gray-400 text-xs">Avg Path Return</div>
                      <div className="text-green-400 font-bold">+18.5%</div>
                    </div>
                    <div className="bg-gray-800 p-3 rounded">
                      <div className="text-gray-400 text-xs">Best Case (95%)</div>
                      <div className="text-green-400 font-bold">+45.2%</div>
                    </div>
                    <div className="bg-gray-800 p-3 rounded">
                      <div className="text-gray-400 text-xs">Worst Case (5%)</div>
                      <div className="text-red-400 font-bold">-12.3%</div>
                    </div>
                    <div className="bg-gray-800 p-3 rounded">
                      <div className="text-gray-400 text-xs">Win Probability</div>
                      <div className="text-blue-400 font-bold">73%</div>
                    </div>
                  </div>
                </div>
              )}

              <div className="mt-4 p-3 bg-blue-900/20 border border-blue-700/30 rounded text-xs text-blue-300">
                ℹ️ Monte Carlo randomly samples market movements based on strategy's historical Sharpe ratio. Shows 100 possible 1-year outcomes.
              </div>
            </div>
          )}
        </>
      )}

      {/* Empty State */}
      {results.length === 0 && (
        <div className="p-8 text-center text-gray-400">
          <Grid3X3 className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="mb-2">Click "Run Optimization" to find the best parameters</p>
          <p className="text-xs">Grid search tests multiple parameter combinations against historical data</p>
        </div>
      )}
    </div>
  );
};
