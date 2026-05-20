import React, { useState, useMemo } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { Chart } from './components/Chart';
import { Indicators } from './components/Indicators';
import { GreeksHeatmap } from './components/GreeksHeatmap';
import { AlertCircle, Wifi, WifiOff } from 'lucide-react';

const TICKERS = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'NVDA', 'SPY'];
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '1d'];

export function App() {
  const [selectedTicker, setSelectedTicker] = useState('AAPL');
  const [selectedTimeframe, setSelectedTimeframe] = useState('1m');
  const { bars, indicators, connected, error } = useWebSocket(selectedTicker);

  // Current price for Greeks calculation
  const currentPrice = useMemo(() => {
    if (bars.length === 0) return 150;
    return bars[bars.length - 1].close;
  }, [bars]);

  return (
    <div className="min-h-screen bg-gray-900 text-gray-100">
      {/* Header */}
      <div className="bg-gray-800 border-b border-gray-700 p-4">
        <div className="max-w-7xl mx-auto">
          <div className="flex justify-between items-center mb-4">
            <h1 className="text-3xl font-bold text-white">StockSignal</h1>
            <div className="flex items-center gap-2">
              {connected ? (
                <>
                  <Wifi className="w-5 h-5 text-green-400" />
                  <span className="text-green-400">Connected</span>
                </>
              ) : (
                <>
                  <WifiOff className="w-5 h-5 text-red-400" />
                  <span className="text-red-400">Disconnected</span>
                </>
              )}
            </div>
          </div>

          {/* Controls */}
          <div className="flex gap-4 items-center">
            {/* Ticker */}
            <div className="flex gap-2 items-center">
              <label className="text-sm font-medium text-gray-400">Ticker:</label>
              <select
                value={selectedTicker}
                onChange={(e) => setSelectedTicker(e.target.value)}
                className="bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none focus:border-blue-500 text-sm"
              >
                {TICKERS.map((ticker) => (
                  <option key={ticker} value={ticker}>
                    {ticker}
                  </option>
                ))}
              </select>
            </div>

            {/* Timeframe */}
            <div className="flex gap-2 items-center">
              <label className="text-sm font-medium text-gray-400">Timeframe:</label>
              <div className="flex gap-1 bg-gray-700 rounded p-1 border border-gray-600">
                {TIMEFRAMES.map((tf) => (
                  <button
                    key={tf}
                    onClick={() => setSelectedTimeframe(tf)}
                    className={`px-3 py-1 text-xs font-medium rounded transition ${
                      selectedTimeframe === tf
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-400 hover:text-gray-200'
                    }`}
                  >
                    {tf}
                  </button>
                ))}
              </div>
            </div>

            {currentPrice > 0 && (
              <div className="ml-auto text-right">
                <div className="text-gray-400 text-sm">Current Price</div>
                <div className="text-2xl font-bold text-white">
                  ${currentPrice.toFixed(2)}
                </div>
              </div>
            )}
          </div>

          {/* Error Display */}
          {error && (
            <div className="mt-3 bg-red-900 border border-red-700 rounded p-3 flex gap-2">
              <AlertCircle className="w-5 h-5 text-red-400 flex-shrink-0" />
              <div className="text-red-100 text-sm">{error}</div>
            </div>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto p-4">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Chart - Left Side (spans 2 columns on large screens) */}
          <div className="lg:col-span-2 space-y-4">
            <Chart bars={bars} indicators={indicators} ticker={selectedTicker} />
          </div>

          {/* Right Sidebar */}
          <div className="space-y-4">
            {/* Indicators */}
            <Indicators indicators={indicators} />

            {/* Greeks Heatmap */}
            <GreeksHeatmap ticker={selectedTicker} spotPrice={currentPrice} />
          </div>
        </div>

        {/* Bottom Info */}
        <div className="mt-8 p-4 bg-gray-800 rounded-lg border border-gray-700 text-sm text-gray-400">
          <p>
            📊 <strong>Week 1 MVP</strong>: Real-time charting with 10 streaming indicators,
            Greeks heatmap, and WebSocket live updates. Built with Tier 1 backend
            integration.
          </p>
        </div>
      </div>
    </div>
  );
}

export default App;
