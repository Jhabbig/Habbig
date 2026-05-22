import React, { useState, useMemo } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import { Chart } from './components/Chart';
import { Indicators } from './components/Indicators';
import { GreeksHeatmap } from './components/GreeksHeatmap';
import { SignalPanel } from './components/SignalPanel';
import { OptionsScan } from './components/OptionsScan';
import { AlertManager } from './components/AlertManager';
import { BacktestPage } from './pages/Backtest';
import { TradingLive } from './pages/TradingLive';
import { Community } from './pages/Community';
import { Analytics } from './pages/Analytics';
import { AdvancedGreeks } from './pages/AdvancedGreeks';
import { MultiAsset } from './pages/MultiAsset';
import { AlertCircle, Wifi, WifiOff, BarChart3, TrendingUp, Zap, Gauge, Users, Activity, Percent, Grid } from 'lucide-react';

const TICKERS = ['AAPL', 'TSLA', 'MSFT', 'GOOGL', 'NVDA', 'SPY'];
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '1d'];

export function App() {
  const [selectedTicker, setSelectedTicker] = useState('AAPL');
  const [selectedTimeframe, setSelectedTimeframe] = useState('1m');
  const [activePage, setActivePage] = useState<'chart' | 'signals' | 'options' | 'advanced' | 'multiasset' | 'trading' | 'analytics' | 'backtest' | 'community'>('chart');
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

      {/* Navigation Tabs */}
      <div className="bg-gray-800 border-b border-gray-700 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4">
          <div className="flex gap-2 items-center overflow-x-auto">
            <button
              onClick={() => setActivePage('chart')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'chart'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <BarChart3 className="w-4 h-4 inline mr-2" />
              Chart
            </button>
            <button
              onClick={() => setActivePage('signals')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'signals'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Zap className="w-4 h-4 inline mr-2" />
              Signals
            </button>
            <button
              onClick={() => setActivePage('options')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'options'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Gauge className="w-4 h-4 inline mr-2" />
              Options
            </button>
            <button
              onClick={() => setActivePage('advanced')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'advanced'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Percent className="w-4 h-4 inline mr-2" />
              Advanced
            </button>
            <button
              onClick={() => setActivePage('multiasset')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'multiasset'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Grid className="w-4 h-4 inline mr-2" />
              Multi-Asset
            </button>
            <button
              onClick={() => setActivePage('trading')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'trading'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <TrendingUp className="w-4 h-4 inline mr-2" />
              Trade Live
            </button>
            <button
              onClick={() => setActivePage('analytics')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'analytics'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Activity className="w-4 h-4 inline mr-2" />
              Analytics
            </button>
            <button
              onClick={() => setActivePage('backtest')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'backtest'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <BarChart3 className="w-4 h-4 inline mr-2" />
              Backtest
            </button>
            <button
              onClick={() => setActivePage('community')}
              className={`py-3 px-4 font-medium border-b-2 transition whitespace-nowrap ${
                activePage === 'community'
                  ? 'border-blue-500 text-blue-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              <Users className="w-4 h-4 inline mr-2" />
              Community
            </button>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto p-4">
        {/* Chart Page */}
        {activePage === 'chart' && (
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
        )}

        {/* Signals Page */}
        {activePage === 'signals' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">AI Trading Signals</h2>
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="lg:col-span-2">
                <SignalPanel ticker={selectedTicker} price={currentPrice} indicators={indicators} />
              </div>
              <div>
                <div className="bg-gray-800 border border-gray-700 rounded-lg p-4">
                  <h3 className="font-semibold text-gray-100 mb-3">Signal Guide</h3>
                  <div className="space-y-2 text-sm text-gray-400">
                    <p>✅ <strong>BUY</strong>: Multiple indicators align bullish</p>
                    <p>❌ <strong>SELL</strong>: Multiple indicators align bearish</p>
                    <p>⏸️ <strong>HOLD</strong>: Indicators mixed or neutral</p>
                    <p className="text-xs text-gray-500 mt-4">Confidence reflects how many indicators agree with the signal.</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Options Scanner Page */}
        {activePage === 'options' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Options Scanner</h2>
            <OptionsScan ticker={selectedTicker} />
          </div>
        )}

        {/* Advanced Greeks Page */}
        {activePage === 'advanced' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Advanced Greeks Analysis</h2>
            <AdvancedGreeks />
          </div>
        )}

        {/* Multi-Asset Page */}
        {activePage === 'multiasset' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Multi-Asset Trading</h2>
            <MultiAsset />
          </div>
        )}

        {/* Trade Live Page */}
        {activePage === 'trading' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Paper Trading</h2>
            <TradingLive currentPrice={currentPrice} />
          </div>
        )}

        {/* Analytics Page */}
        {activePage === 'analytics' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Advanced Analytics</h2>
            <Analytics />
          </div>
        )}

        {/* Backtest Page */}
        {activePage === 'backtest' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Backtest Strategy</h2>
            <BacktestPage />
          </div>
        )}

        {/* Community Page */}
        {activePage === 'community' && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold text-white mb-4">Community</h2>
            <Community />
          </div>
        )}
      </div>

      {/* Global Alert Manager */}
      <AlertManager />
    </div>
  );
}

export default App;
